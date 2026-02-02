#!/bin/bash
#SBATCH --job-name=emb_eval
#SBATCH --output=logs/eval_%j.log
#SBATCH --error=logs/eval_%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --cluster=smp

# 1. Activate Conda (skip module load)
source ~/.bashrc
conda activate whg

# 2. Set Working Directory
# We must be in the 'elastic' folder for "python -m testing.evaluate_embeddings" to work
cd /ix1/ishi/elastic

# 3. LOCATE STAGING ELASTICSEARCH
ENV_FILE="/ix1/ishi/elastic/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

INFO_FILE="${STAGING_INFO_FILE:-/ix1/ishi/es/staging/es_info.sh}"

if [ -f "$INFO_FILE" ]; then
    source "$INFO_FILE"
    echo "Found Staging ES at: $ES_URL"
else
    echo "ERROR: Staging ES info file not found at $INFO_FILE"
    echo "Is the es-staging job running?"
    exit 1
fi

# 4. Run Evaluation
echo "Starting evaluation on $(hostname)"
echo "Connecting to ES at: $ES_URL"
echo "Working directory: $(pwd)"

python -m testing.evaluate_embeddings \
    --es-host "$ES_URL" \
    --model-path /ix1/ishi/models/phonetic/checkpoints/final_model_b.pt \
    --samples 50 \
    --neighbours 15 \
    --output article/noise-evaluation.tex

echo "Job finished"