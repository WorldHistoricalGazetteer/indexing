#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 2
#SBATCH --mem=12G
#SBATCH -t 00:30:00
#SBATCH -J pin_cov
#SBATCH -o /vast/ishi/gb1900/probe/hisam/pin_coverage.log
# Prevalence of the distinctive-font categories in the GB1900 gazetteer — the fact that decides whether
# pin-prompted localisation alone is enough for Phase A.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/hisam
cd /vast/ishi/gb1900/probe/hisam
python pin_category_coverage.py
