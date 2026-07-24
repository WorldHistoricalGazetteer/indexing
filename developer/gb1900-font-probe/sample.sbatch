#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 2
#SBATCH --mem=16G
#SBATCH -t 00:40:00
#SBATCH -J build_sample
#SBATCH -o /vast/ishi/gb1900/probe/hisam/build_sample.log
# Choose the stratified Phase-A sample (regions, not scattered pins). CPU only.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/hisam
cd /vast/ishi/gb1900/probe/hisam
python build_sample.py --target "${TARGET:-20000}" --per-category "${PERCAT:-600}"
