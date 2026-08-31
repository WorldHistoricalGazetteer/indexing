#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 8
#SBATCH --mem=24G
#SBATCH -t 6-00:00:00
#SBATCH -J spotall
#SBATCH --array=0-15
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/spotall_%a.log
# Spot ALL 35,514 regions -- the whole of Great Britain -- from the local tile corpus.
#
# The previous sweep covered 1,307 regions, 3.7% of the series. That was a sample, and it was a sample
# because on-demand tile fetching made a region cost minutes to an hour. With the corpus local a mosaic is
# ~7s, so a region is ~60-70s of GPU and the full sweep becomes tractable.
#
# ONE MODEL LOAD PER SHARD. spot_sheet now takes --centres/--shard/--of and loops internally. Loading
# MapTextRunner per region cost 20-40s, which was noise when tiles took minutes and would have been a large
# share of the total now that they do not.
#
# Writes to spot2/ so the existing pass survives for comparison: identical weights on identical imagery
# should reproduce the boxes and merely ADD the model's own baseline (gline).
set -u
# Force UTF-8 regardless of the node. Python encodes text files with the LOCALE charset, and one
# preempt node (gpu-n61) has no UTF-8 locale — so the same code that ran clean on fifteen shards
# failed 34% of regions on that one, throwing away nine mosaics of inference each time. Don't rely
# on the compute node being configured the way the last one was.
export LC_ALL=C.UTF-8 LANG=C.UTF-8 PYTHONUTF8=1
cd /vast/ishi/gb1900/probe/font
export SPOT_OUT=/vast/ishi/gb1900/edition/spot2
# The corpus is verified complete (2,366/2,366 blocks) and IS the world for this sweep. Without this the
# mosaic grid's overrun past each region edge becomes live S3 traffic — harmless for an interior region
# whose neighbour cached those tiles, ruinous at the coast where there is no neighbour. The first launch
# measured 742s on Unst, Shetland against a ~65s budget; at that rate the sweep is ~27 days, not ~40h.
export SPOT_NO_FETCH=1
mkdir -p "$SPOT_OUT"
OMP_NUM_THREADS=6 /vast/ishi/envs/mapreader/bin/python spot_sheet.py \
  --centres centres_all.txt --shard $SLURM_ARRAY_TASK_ID --of 16 --r 8
