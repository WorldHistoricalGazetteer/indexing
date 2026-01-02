#!/bin/bash
#SBATCH --job-name=emb_eval
#SBATCH --output=logs/eval_%j.log
#SBATCH --error=logs/eval_%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --cluster=smp      # Optional: Explicitly target SMP/HTC if required, otherwise remove

# 1. Load environment
module load python/3.11.5

# 2. Activate Conda
source ~/.bashrc
conda activate whg

# 3. LOCATE STAGING ELASTICSEARCH
# We need to connect to the ES instance running in your 'es-staging' job.
# The staging script writes connection info to a specific file.
# (Adjust this path if STAGING_INFO_FILE is defined elsewhere in your .env)
ENV_FILE="/ix1/whcdh/elastic/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

INFO_FILE="${STAGING_INFO_FILE:-/ix1/whcdh/es/staging/es_info.sh}"

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

python -m testing.evaluate_embeddings \
    --es-host "$ES_URL" \
    --model-path /ix1/whcdh/models/phonetic/checkpoints/final_model_b.pt \
    --noise \
    --noise-level 0.3 \
    --samples 50 \
    --neighbours 15 \
    --output article/noise-evaluation.tex

echo "Job finished"