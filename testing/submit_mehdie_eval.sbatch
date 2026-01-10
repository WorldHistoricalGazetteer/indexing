#!/bin/bash
#SBATCH --job-name=mehdie-eval
#SBATCH --output=/ix1/whcdh/es/staging-logs/mehdie_eval_%j.out
#SBATCH --error=/ix1/whcdh/es/staging-logs/mehdie_eval_%j.err
#SBATCH --time=4:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=4
#SBATCH --partition=htc

# MEHDIE Benchmark Evaluation
# Runs Symphonym model against the MEHDIE Hebrew-Arabic toponym benchmark

echo "=============================================="
echo "MEHDIE BENCHMARK EVALUATION"
echo "=============================================="
echo "Job Started: $(date)"
echo "Node: $(hostname)"
echo "Working Dir: $(pwd)"
echo ""

# Activate conda environment
source ~/.bashrc
conda activate whg

# Change to repository directory
cd /ix1/whcdh/elastic

# Run evaluation
python -m testing.run_mehdie_evaluation \
    --testsets testing/mehdie-testsets \
    --version v3 \
    --device cpu \
    --output /ix1/whcdh/es/staging-logs/mehdie_results_${SLURM_JOB_ID}.json

echo ""
echo "Job Completed: $(date)"

