#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 6
#SBATCH --mem=32G
#SBATCH -t 1-00:00:00
#SBATCH -J pins_array
#SBATCH --array=0-7
#SBATCH -o /vast/ishi/gb1900/probe/hisam/array_%a.log
# Phase A scale-out: pin-prompted localisation over the stratified sample (centres_sample.txt).
# Driver-free and resumable, same shape as spot_full.sbatch: task S takes every 8th line, and a region whose
# pins_<tag>.jsonl already exists and is non-empty is skipped, so re-submitting continues where it stopped.
# GPU nodes have outbound network, so --fetch pulls uncached z17 tiles on demand.
#
# NB Hi-SAM localisation is ~0.4s/window, so unlike the MapReader spot pass these jobs are TILE-FETCH bound.
# Keep the array narrow (8) to stay under S3's throttle rather than wide-and-retrying.
set -u
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate /vast/ishi/envs/hisam
cd /vast/ishi/gb1900/probe/hisam
S=$SLURM_ARRAY_TASK_ID
N=${SLURM_ARRAY_TASK_COUNT:-8}
CENTRES=${CENTRES:-/vast/ishi/gb1900/probe/font/centres_sample.txt}
OUTDIR=${OUTDIR:-/vast/ishi/gb1900/edition/pins}
awk -v s="$S" -v n="$N" 'NR%n==s' "$CENTRES" | while read lon lat tag _; do
  [ -z "$lon" ] && continue
  pf="$OUTDIR/pins_$tag.jsonl"
  [ -s "$pf" ] && continue                  # non-empty only; an empty file is a prior failure worth retrying
  # Wrap each region: S3 sockets can hang past urlopen's timeout (the failure mode that stalled the MapReader
  # pass for hours). A 17x17-tile region is 289 tiles + ~81 windows, so 1800s is generous but not unbounded.
  timeout -k 60 1800 python hisam_pins.py --lon "$lon" --lat "$lat" --tag "$tag" --r "${R:-8}" \
    --fetch --out-dir "$OUTDIR" > "/tmp/pins_$tag.log" 2>&1 || echo "FAIL $tag rc=$?"
done
echo "SHARD $S DONE $(date)"
