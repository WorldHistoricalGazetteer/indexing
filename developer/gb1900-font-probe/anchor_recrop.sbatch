#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 6
#SBATCH --mem=32G
#SBATCH -t 01:00:00
#SBATCH -J anchor_recrop
#SBATCH -o /vast/ishi/gb1900/probe/hisam/anchor_recrop_%j.log
# Phase B: re-derive the sig-anchor discriminator under the Hi-SAM box convention.
# Two stages because the SAM and MapReader models live in separate envs; npz is the handoff.
# NO `set -u`: the mapreader env's activate hook dereferences SYS_SYSROOT unset and dies under nounset.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
cd /vast/ishi/gb1900/probe/hisam
CROPS=/vast/ishi/gb1900/edition/spot/anchor_crops_hisam.npz

if [ ! -s "$CROPS" ]; then                 # stage 1 is resumable — don't re-encode 189 windows to rerun stage 2
  conda activate /vast/ishi/envs/hisam
  python anchor_recrop.py || exit 1
fi

conda activate /vast/ishi/envs/mapreader
python anchor_recrop_readout.py
