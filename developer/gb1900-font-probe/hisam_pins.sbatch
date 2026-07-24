#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 6
#SBATCH --mem=32G
#SBATCH -t 02:00:00
#SBATCH -J hisam_pins
#SBATCH -o /vast/ishi/gb1900/probe/hisam/pins_%j.log
# Pin-prompted Hi-SAM detection over one region, then the validation pass against MapReader's boxes.
# Defaults to the Oxford validation region (dense labels, 928 MapReader boxes, tiles already cached).
# Override with --export=ALL,TAG=...,LON=...,LAT=...
set -u
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/hisam
cd /vast/ishi/gb1900/probe/hisam
TAG=${TAG:-gb_4338_2896}; LON=${LON:--1.25931}; LAT=${LAT:-51.74149}; R=${R:-8}; SNAP=${SNAP:-0}
python hisam_pins.py --tag "$TAG" --lon "$LON" --lat "$LAT" --r "$R" --snap "$SNAP" ${FETCH:+--fetch}
python validate_pins.py --tag "$TAG"
