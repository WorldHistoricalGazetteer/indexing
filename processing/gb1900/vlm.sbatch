#!/bin/bash
# GB1900 Tier-1 typography+text VLM pass (scaffolding; adapts GOTW's proven
# CRC vLLM pattern — /home/stephen/PycharmProjects/GOTW/process/submit_vlm_slurm.py).
#
# GPU Slurm ARRAY: each task starts its OWN `vllm serve` on a unique port, waits
# for startup, runs inference over its crop-shard → per-shard resumable JSONL.
# Model reads each label crop and emits (plan §4.2 schema): os_style (documented
# OS code), case/size_band/tracking, is_water_or_antiquity, legible, AND vlm_text
# (its own reading of the label — our replacement for the curated ~30k corrections,
# recorded as a provenance edit, plan §11.1). Preserves gb:<pin_id>.
#
# Submit (from crc0; fall back crc1/2/3), NEVER a login-node run:
#   sbatch -M gpu --account=ishi --array=0-<N-1> processing/gb1900/vlm.sbatch <crops_dir> <out_dir>
#
#SBATCH --job-name=gb1900-vlm
#SBATCH --account=ishi
#SBATCH --partition=h200
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=08:00:00
#SBATCH --requeue
# Walltime is generous (QOS allows up to 3 days); the real limit is SHARD SIZE.
# At ~1-2 crops/sec on h200, keep shards to ~15-20k crops (~1.5-2.5h). Per-shard
# output is resumable (infer skips done pin_ids), so an overrun just resumes.
#SBATCH --output=/vast/ishi/gb1900/logs/vlm-%A_%a.out
# Submit from crc0 (has Slurm; pitt/gateway VM does not): sbatch -M gpu ...
# h200 (141GB) holds Qwen2.5-VL-72B-AWQ on ONE GPU → TP=1, no multi-GPU fragility.
# This matches GOTW's proven recipe (process/submit_vlm_slurm.py).

set -uo pipefail
MANIFEST="${1:?usage: sbatch gb1900_vlm.sbatch <crop_manifest.jsonl> <out_dir>}"
OUT_DIR="${2:?}"
mkdir -p "$OUT_DIR" /vast/ishi/gb1900/logs

# Env — GOTW-provisioned vLLM env + HF cache on /vast (verified present 2026-07-17).
# Activate conda BEFORE set -u; try CRC-compute and VM conda locations in turn.
set +u
for CS in /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh \
          /ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh \
          /home/gazetteer/miniconda/etc/profile.d/conda.sh; do
  [ -f "$CS" ] && { source "$CS"; break; }
done
conda activate /vast/ishi/envs/vllm
set -u
module load cuda/12.8.0 2>/dev/null || true
export HF_HOME=/vast/ishi/hf_cache
# Per-job cache isolation (GOTW pattern, process/submit_vlm_slurm.py). A SHARED
# ~/.cache/vllm poisoned by an earlier run broke every subsequent EngineCore
# load_model (root cause, 2026-07-17). Fresh per-job dirs for TMPDIR / XDG cache /
# vLLM compile cache prevent that; the node's /tmp is wiped after the job.
RUN=/tmp/gb1900vlm_${SLURM_JOB_ID}_${SLURM_ARRAY_TASK_ID:-0}
mkdir -p "$RUN"
export TMPDIR="$RUN" XDG_CACHE_HOME="$RUN" VLLM_CACHE_ROOT="$RUN"

MODEL="Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
PORT=$((18900 + (SLURM_ARRAY_JOB_ID % 700) + SLURM_ARRAY_TASK_ID))
NGPU=$(echo "${SLURM_GPUS_ON_NODE:-2}")

# 1) serve. --host 127.0.0.1 forces IPv4 (GOTW does this) — without it vLLM binds
# the node hostname (IPv6) → the c10d "Address family not supported" error.
# gpu:1 on h200 → tensor-parallel-size 1 (NGPU=1), no multiproc TP path.
vllm serve "$MODEL" --host 127.0.0.1 --port "$PORT" --tensor-parallel-size "$NGPU" \
  --max-model-len 16384 --gpu-memory-utilization 0.92 --served-model-name "$MODEL" \
  > "/vast/ishi/gb1900/logs/vllm-${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}.log" 2>&1 &
VLLM_PID=$!
# wait for startup
for _ in $(seq 1 120); do
  curl -s "http://localhost:${PORT}/v1/models" >/dev/null 2>&1 && break
  kill -0 "$VLLM_PID" 2>/dev/null || break   # server died — stop waiting
  sleep 5
done
# HEALTH GATE: if vLLM never came up, exit non-zero so --requeue retries (a fresh
# attempt usually gets a clean multiproc init) instead of silently writing nothing.
if ! curl -s "http://localhost:${PORT}/v1/models" >/dev/null 2>&1; then
  echo "FATAL: vLLM engine did not start (see vllm log) — exiting 1 to requeue" >&2
  kill "$VLLM_PID" 2>/dev/null || true
  exit 1
fi

# 2) infer over this array task's crop-shard → per-shard JSONL (resumable).
#    (Python inference client mirrors GOTW triage_pages.py: OpenAI-compatible
#    /v1/chat/completions, response_format json_schema strict, temperature 0,
#    ThreadPoolExecutor(32), base64 crops, regex salvage; keyed on gb:<pin_id>.)
python -m processing.gb1900.vlm_infer \
  --crops "$MANIFEST" --shard "$SLURM_ARRAY_TASK_ID" --nshards "${SLURM_ARRAY_TASK_COUNT:-1}" \
  --endpoint "http://localhost:${PORT}/v1" --model "$MODEL" \
  --out "${OUT_DIR}/shard-${SLURM_ARRAY_TASK_ID}.jsonl"

kill "$VLLM_PID" 2>/dev/null || true
