#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 12
#SBATCH --mem=48G
#SBATCH -t 02:00:00
#SBATCH -J descextract
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/descextract_%a.log
set -e
cd /vast/ishi/gb1900/probe/font
/vast/ishi/envs/mapreader/bin/python extract_descriptors.py
