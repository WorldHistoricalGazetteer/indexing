#!/bin/bash
#SBATCH --job-name=whg-verify-un
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=96G
#SBATCH --time=04:00:00
#SBATCH --output=/vast/ishi/elastic/logs/whg-verify-un-%j.out
#SBATCH --error=/vast/ishi/elastic/logs/whg-verify-un-%j.err

# Loads every country's full geometry and cross-tests representative points,
# so it needs a compute node — never a login node.

source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
cd /vast/ishi/elastic

python -u -m processing.verify_un_geometries \
    --json-out /vast/ishi/elastic/logs/un-geometry-report-${SLURM_JOB_ID}.json
