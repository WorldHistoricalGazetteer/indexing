#!/bin/bash

# Execution: cd processing/ && sbatch optimise.slurm

#SBATCH --job-name=optimise_places
#SBATCH --output=optimise_places_%j.log
#SBATCH --error=optimise_places_%j.err
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --partition=htc

# 1. Load Environment
CONDA_SETUP="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP" ]; then
    CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
source "$CONDA_SETUP"
conda activate whg

# 2. Navigate to Project Root
# We start in 'processing/', so we move UP one level to the project root.
cd $SLURM_SUBMIT_DIR/..

echo "======================================================"
echo "Starting Optimization Job at $(date)"
echo "Project Root: $(pwd)"
echo "Running on node: $(hostname)"
echo "======================================================"

# 3. Run the script
python -u processing/optimise_places.py

echo "======================================================"
echo "Job finished at $(date)"
echo "======================================================"