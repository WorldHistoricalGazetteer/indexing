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
# - Uses ranking-based metrics (Recall@K, MRR) for embedding-based systems

echo "========================================"
echo "PART 1: RANKING-BASED EVALUATION"
echo "========================================"
echo "Running ranking evaluation (Recall@1, Recall@5, Recall@10, Recall@20, MRR)..."
echo ""

python -m testing.run_mehdie_evaluation \
    --testsets testing/mehdie-testsets \
    --version v6 \
    --device cpu \
    --include-baselines \
    --output /ix1/ishi/es/staging-logs/mehdie_results_v6_ranking_${SLURM_JOB_ID}.json

echo ""
echo "========================================"
echo "PART 2: THRESHOLD-BASED EVALUATION"
echo "========================================"
echo "Running threshold evaluation (F-5, Precision, Recall for comparison with paper)..."
echo ""

python -m testing.run_mehdie_evaluation \
    --testsets testing/mehdie-testsets \
    --version v6 \
    --device cpu \
    --include-baselines \
    --use-thresholds \
    --output /ix1/ishi/es/staging-logs/mehdie_results_v6_thresholds_${SLURM_JOB_ID}.json

echo ""
echo "Job Completed: $(date)"
