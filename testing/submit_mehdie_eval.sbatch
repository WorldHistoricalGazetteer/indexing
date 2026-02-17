#!/bin/bash
#SBATCH --job-name=mehdie-eval
#SBATCH --output=/ix1/ishi/es/staging-logs/mehdie_eval_%j.out
#SBATCH --error=/ix1/ishi/es/staging-logs/mehdie_eval_%j.err
#SBATCH --time=1:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=8
#SBATCH --cluster=htc

# MEHDIE Benchmark Evaluation (CPU)
# Runs Symphonym model against the MEHDIE Hebrew-Arabic toponym benchmark

echo "=============================================="
echo "MEHDIE BENCHMARK EVALUATION (CPU)"
echo "=============================================="
echo "Job Started: $(date)"
echo "Node: $(hostname)"
echo "CPUs: ${SLURM_CPUS_PER_TASK}"
echo "Working Dir: $(pwd)"
echo ""

# Activate conda environment
source ~/.bashrc
conda activate whg

# Change to repository directory
cd /ix1/ishi/elastic

# Run evaluation with CPU
# Full comparison with MEHDIE paper:
# - Includes Levenshtein and Jaro-Winkler baselines
# - Uses threshold-based metrics (Precision, Recall, F-5) for direct comparison
# - Also includes ranking-based metrics (Recall@K, MRR)
python -m testing.run_mehdie_evaluation \
    --testsets testing/mehdie-testsets \
    --version v6 \
    --device cpu \
    --include-baselines \
    --use-thresholds \
    --output /ix1/ishi/es/staging-logs/mehdie_results_v6_${SLURM_JOB_ID}.json

echo ""
echo "Job Completed: $(date)"
