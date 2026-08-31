#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 00:20:00
#SBATCH -J labelui
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/labelui.log
set -e
cd /vast/ishi/gb1900/probe/font
/vast/ishi/envs/mapreader/bin/python build_label_ui.py --n "${NCARDS:-100}" --mode "${MODE:-auto}"
echo LABELUIDONE
