#!/bin/bash
#SBATCH -M htc
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-l
#SBATCH -c 4
#SBATCH --mem=8G
#SBATCH -t 3-00:00:00
#SBATCH -J corpus
#SBATCH --array=0-15
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/corpus_%a.log
# Fetch the whole z17 corpus into per-block SQLite on /ix1. 16 tasks x 4 req/s = ~64/s aggregate, which is
# a fraction of what the earlier 8 spotting workers were doing in bursts (they issued 64 parallel fetches
# each and drew 503 SlowDown). Resumable: a block already holding every tile it wants is skipped, so a
# walltime kill costs the block in flight and nothing else.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/mapreader
cd /vast/ishi/gb1900/probe/font
python build_tile_corpus.py --shard $SLURM_ARRAY_TASK_ID --of 16 --rps 4 --workers 8
