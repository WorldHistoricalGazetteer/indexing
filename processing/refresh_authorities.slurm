#!/bin/bash
#SBATCH --job-name=refresh_authorities
#SBATCH --output=/ix1/whcdh/logs/refresh_authorities_%j.log
#SBATCH --error=/ix1/whcdh/logs/refresh_authorities_%j.err
#SBATCH --time=12:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=4

# Activate conda environment
source /ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg

# Run the update script, forcing refresh of all authorities
# python -m processing.fetch_authorities --age 0

# OR, run the update script without forcing refresh (keeps any files less than 1 year old)
python -m processing.fetch_authorities
