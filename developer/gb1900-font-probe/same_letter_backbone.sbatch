#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 4
#SBATCH --mem=32G
#SBATCH -t 00:20:00
#SBATCH -J bbprobe
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/slbb.log
set -e
cd /vast/ishi/gb1900/probe/font
/vast/ishi/envs/mapreader/bin/python same_letter_backbone.py
echo SLBBDONE
