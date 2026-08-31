#!/bin/bash
#SBATCH -J prefetch
#SBATCH --account=ishi
#SBATCH --array=0-3
#SBATCH -c 8
#SBATCH --mem=4G
#SBATCH -t 1-00:00:00
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/prefetch_%a.log
cd /vast/ishi/gb1900/probe/font
FCTILES=/vast/ishi/gb1900/tiles17 /vast/ishi/envs/mapreader/bin/python prefetch_tiles.py --workers 6 --nshards 4
echo "PREFETCH SHARD $SLURM_ARRAY_TASK_ID DONE $(date)"
