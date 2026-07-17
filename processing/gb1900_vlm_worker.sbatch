#!/bin/bash
# GB1900 STAMP — AUTONOMOUS VLM worker pool (h200). Submit ONCE as an array; each
# task loads vLLM once, then PULLS batch manifests the pitt cropper produces
# (batches/batch_*.jsonl) and infers them until the cropper writes batches/DONE.
# No login-node process, no operator in the loop. Model loads once per worker
# (not once per batch). Correctness from idempotent infer (skips done pin_ids), so
# the mkdir-lock is only an efficiency hint; on drain, workers steal orphans.
#
#   sbatch -M gpu --array=0-5 processing/gb1900_vlm_worker.sbatch \
#          /vast/ishi/gb1900/edition/batches /vast/ishi/gb1900/edition/vlm
#
#SBATCH --job-name=gbstamp-vlm
#SBATCH --account=ishi
#SBATCH --partition=h200
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=2-00:00:00
#SBATCH --requeue
#SBATCH --output=/vast/ishi/gb1900/logs/vlmworker-%A_%a.out

set -uo pipefail
BATCH_DIR="${1:?usage: sbatch gb1900_vlm_worker.sbatch <batch_dir> <out_dir>}"
OUT_DIR="${2:?}"
REPO="/vast/ishi/elastic"
mkdir -p "$OUT_DIR" /vast/ishi/gb1900/logs
cd "$REPO"

# --- env (GOTW recipe): per-job cache isolation + IPv4 host ---
set +u
for CS in /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh \
          /ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh \
          /home/gazetteer/miniconda/etc/profile.d/conda.sh; do
  [ -f "$CS" ] && { source "$CS"; break; }
done
conda activate /vast/ishi/envs/vllm
set -u
module load cuda/12.8.0 2>/dev/null || true
RUN=/tmp/gbstampvlm_${SLURM_JOB_ID}_${SLURM_ARRAY_TASK_ID}
mkdir -p "$RUN"
export HF_HOME=/vast/ishi/hf_cache TMPDIR="$RUN" XDG_CACHE_HOME="$RUN" VLLM_CACHE_ROOT="$RUN"

MODEL="Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
PORT=$((18900 + (SLURM_ARRAY_JOB_ID % 700) + SLURM_ARRAY_TASK_ID))
vllm serve "$MODEL" --host 127.0.0.1 --port "$PORT" --tensor-parallel-size "${TP:-1}" \
  --max-model-len 16384 --gpu-memory-utilization 0.92 --served-model-name "$MODEL" \
  > "/vast/ishi/gb1900/logs/vllm-${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}.log" 2>&1 &
VLLM_PID=$!
for _ in $(seq 1 180); do
  curl -s "http://localhost:${PORT}/v1/models" >/dev/null 2>&1 && break
  kill -0 "$VLLM_PID" 2>/dev/null || break
  sleep 5
done
if ! curl -s "http://localhost:${PORT}/v1/models" >/dev/null 2>&1; then
  echo "FATAL: vLLM did not start — exit 1 (requeue)" >&2; kill "$VLLM_PID" 2>/dev/null; exit 1
fi
ENDPOINT="http://localhost:${PORT}/v1"

infer() {   # $1 = manifest, $2 = name
  python -m processing.gb1900_vlm_infer --crops "$1" \
    --endpoint "$ENDPOINT" --model "$MODEL" --out "$OUT_DIR/$2/shard-0.jsonl" \
    && touch "$OUT_DIR/$2.done"
}

echo "[worker ${SLURM_ARRAY_TASK_ID}] vLLM up on $PORT; pulling batches from $BATCH_DIR"
idle=0
while true; do
  got=""
  shopt -s nullglob
  for man in "$BATCH_DIR"/batch_*.jsonl; do
    name=$(basename "$man" .jsonl)
    [ -f "$OUT_DIR/$name.done" ] && continue
    if mkdir "$OUT_DIR/$name.lock" 2>/dev/null; then got="$man"; gname="$name"; break; fi
  done
  if [ -n "$got" ]; then
    echo "[worker ${SLURM_ARRAY_TASK_ID}] $gname"; infer "$got" "$gname"; idle=0; continue
  fi
  if [ -f "$BATCH_DIR/DONE" ]; then
    # drain: steal any batch still without a .done (orphaned by a dead worker)
    orphan=""
    for man in "$BATCH_DIR"/batch_*.jsonl; do
      n=$(basename "$man" .jsonl); [ -f "$OUT_DIR/$n.done" ] || { orphan="$man"; oname="$n"; break; }
    done
    if [ -n "$orphan" ]; then echo "[worker] steal $oname"; infer "$orphan" "$oname"; continue; fi
    echo "[worker ${SLURM_ARRAY_TASK_ID}] cropper DONE and no batches left — exit"; break
  fi
  idle=$((idle+1)); sleep 60
done

# The LAST worker out builds the GB-STAMP edition inline (whoever wins the lock),
# so no separate reconcile job / no idle node / no cross-cluster dependency.
ED=/vast/ishi/gb1900/edition
if mkdir "$OUT_DIR/reconcile.lock" 2>/dev/null; then
  echo "[worker ${SLURM_ARRAY_TASK_ID}] running final reconcile → GB-STAMP edition"
  cat "$OUT_DIR"/batch_*/shard-0.jsonl > "$ED/national_vlm.jsonl" 2>/dev/null || true
  echo "merged VLM records: $(wc -l < $ED/national_vlm.jsonl 2>/dev/null || echo 0)"
  python3 -m processing.gb1900_reconcile --tier0 "$ED/national_typed.jsonl" \
    --vlm "$ED/national_vlm.jsonl" --out "$ED/gb-stamp_edition.jsonl" --version gbtype-v1
  echo "[worker] GB-STAMP edition → $ED/gb-stamp_edition.jsonl"
fi
kill "$VLLM_PID" 2>/dev/null || true
