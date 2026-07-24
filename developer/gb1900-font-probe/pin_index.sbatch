#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 2
#SBATCH --mem=12G
#SBATCH -t 00:40:00
#SBATCH -J pin_index
#SBATCH -o /vast/ishi/gb1900/probe/hisam/pin_index.log
# One-off: parse the 700 MB UTF-16 GB1900 gazetteer into a z17-pixel pin index (/vast/ishi/gb1900/pins_z17.npz).
# CPU/IO only — no GPU, and no login-node work.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/hisam
cd /vast/ishi/gb1900/probe/hisam
python build_pin_index.py
