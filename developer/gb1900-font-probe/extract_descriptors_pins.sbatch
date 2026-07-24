#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 6
#SBATCH --mem=32G
#SBATCH -t 08:00:00
#SBATCH -J desc_pins
#SBATCH --array=0-7
#SBATCH -o /vast/ishi/gb1900/probe/hisam/desc_pins_%a.log
# Descriptor bank over the pin-prompted detections (Hi-SAM LINE crops).
# NO `set -u` — the mapreader env's activate hook dies under nounset.
# Run AFTER pins_array has finished: region files are assigned whole, so a half-written corpus shards unevenly.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/mapreader
cd /vast/ishi/gb1900/probe/hisam
python extract_descriptors_pins.py
