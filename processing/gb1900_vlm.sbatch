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
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=80G
#SBATCH --time=04:00:00
#SBATCH --requeue
#SBATCH --output=/vast/ishi/gb1900/logs/vlm-%A_%a.out

set -uo pipefail
CROPS_DIR="${1:?usage: sbatch gb1900_vlm.sbatch <crops_dir> <out_dir>}"
OUT_DIR="${2:?}"
mkdir -p "$OUT_DIR" /vast/ishi/gb1900/logs

# Env — gazetteer-independent; use the GOTW-provisioned vLLM env + HF cache on /vast
# (verified present 2026-07-17). Activate conda BEFORE any set -u tightening.
set +u
source /home/gazetteer/miniconda/etc/profile.d/conda.sh 2>/dev/null || \
  source "$(dirname "$(command -v conda)")/../etc/profile.d/conda.sh"
conda activate /vast/ishi/envs/vllm
set -u
module load cuda/12.8.0 2>/dev/null || true
export HF_HOME=/vast/ishi/hf_cache

MODEL="Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
PORT=$((18900 + (SLURM_ARRAY_JOB_ID % 700) + SLURM_ARRAY_TASK_ID))

# 1) serve
vllm serve "$MODEL" --port "$PORT" --max-model-len 8192 --gpu-memory-utilization 0.92 \
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
  --crops "$CROPS_DIR" --shard "$SLURM_ARRAY_TASK_ID" --nshards "$SLURM_ARRAY_TASK_COUNT" \
  --endpoint "http://localhost:${PORT}/v1" --model "$MODEL" \
  --out "${OUT_DIR}/shard-${SLURM_ARRAY_TASK_ID}.jsonl"

kill "$VLLM_PID" 2>/dev/null || true
