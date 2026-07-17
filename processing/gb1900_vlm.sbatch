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
#   sbatch -M gpu --account=ishi --array=0-<N-1> processing/gb1900_vlm.sbatch <crops_dir> <out_dir>
#
#SBATCH --job-name=gb1900-vlm
#SBATCH --account=ishi
#SBATCH --partition=a100
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-task=12
#SBATCH --mem=120G
#SBATCH --time=04:00:00
#SBATCH --requeue
#SBATCH --output=/vast/ishi/gb1900/logs/vlm-%A_%a.out
# Submit from crc0 (has Slurm; pitt/gateway VM does not): sbatch -M gpu ...

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

MODEL="Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
PORT=$((18900 + (SLURM_ARRAY_JOB_ID % 700) + SLURM_ARRAY_TASK_ID))
NGPU=$(echo "${SLURM_GPUS_ON_NODE:-2}")

# 1) serve (tensor-parallel across the allocated A100s for the 72B AWQ model)
vllm serve "$MODEL" --port "$PORT" --tensor-parallel-size "$NGPU" \
  --max-model-len 8192 --gpu-memory-utilization 0.92 --limit-mm-per-prompt image=1 \
  > "/vast/ishi/gb1900/logs/vllm-${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}.log" 2>&1 &
VLLM_PID=$!
# wait for startup
for _ in $(seq 1 120); do
  curl -s "http://localhost:${PORT}/v1/models" >/dev/null 2>&1 && break; sleep 5
done

# 2) infer over this array task's crop-shard → per-shard JSONL (resumable).
#    (Python inference client mirrors GOTW triage_pages.py: OpenAI-compatible
#    /v1/chat/completions, response_format json_schema strict, temperature 0,
#    ThreadPoolExecutor(32), base64 crops, regex salvage; keyed on gb:<pin_id>.)
python -m processing.gb1900_vlm_infer \
  --crops "$MANIFEST" --shard "$SLURM_ARRAY_TASK_ID" --nshards "${SLURM_ARRAY_TASK_COUNT:-1}" \
  --endpoint "http://localhost:${PORT}/v1" --model "$MODEL" \
  --out "${OUT_DIR}/shard-${SLURM_ARRAY_TASK_ID}.jsonl"

kill "$VLLM_PID" 2>/dev/null || true
