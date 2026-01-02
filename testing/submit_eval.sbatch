#!/bin/bash
#SBATCH --job-name=emb_noise_eval
#SBATCH --output=logs/noise_eval_%j.log
#SBATCH --error=logs/noise_eval_%j.err
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1           # Request 1 GPU
#SBATCH --cpus-per-task=4      # Request 4 CPU cores
#SBATCH --mem=16G              # Request 16GB RAM
#SBATCH --time=02:00:00        # Set a 2-hour limit
#SBATCH --account=whcdh        # Your account group (inferred from path)
#SBATCH --partition=gpu        # Adjust if your cluster uses 'gtx1080', 'v100', etc.

# 1. Load necessary modules (adjust based on your cluster, e.g., python/3.11)
module load python/3.11.5
module load cuda/11.8

# 2. Activate your conda environment
source ~/.bashrc
conda activate whg

# 3. Create output directory for logs if it doesn't exist
mkdir -p logs

# 4. Run the script
echo "Starting evaluation on $(hostname)"
python -m testing.evaluate_embeddings \
    --model-path /ix1/whcdh/models/phonetic/checkpoints/final_model_b.pt \
    --noise \
    --noise-level 0.3 \
    --samples 50 \
    --neighbours 15 \
    --output article/noise-evaluation.tex

echo "Job finished"