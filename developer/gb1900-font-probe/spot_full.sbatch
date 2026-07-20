#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH -c 6
#SBATCH --mem=20G
#SBATCH -t 6-00:00:00
#SBATCH -J spot_full
#SBATCH --array=0-15
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/full_%a.log
# FULL-COVERAGE spotting, driver-free. GPU nodes have outbound network (verified 2026-07-20), so spot_sheet
# fetches z17 tiles on-demand and --cleanup ARCHIVES each region's tiles to /ix1 once its boxes are written,
# so /vast never accumulates tiles AND the tiles survive for a later font-classify pass (font_classify reads
# /ix1, no NLS re-fetch). Font classification is a SEPARATE pass (not inline: loading the CPU encoder inside
# the CUDA spot process is unreliable). 16 array tasks each take 1/16 of centres_all.txt (NR%16). Resumable:
# a region whose boxes_<tag>.jsonl exists (even empty) is skipped, so re-submitting continues.
set -u
cd /vast/ishi/gb1900/probe/font
MRPY=/vast/ishi/envs/mapreader/bin/python
S=$SLURM_ARRAY_TASK_ID
awk "NR%16==$S" centres_all.txt | while read lon lat tag; do
  [ -z "$lon" ] && continue
  bf=/vast/ishi/gb1900/edition/spot/boxes_$tag.jsonl
  [ -f "$bf" ] && continue
  OMP_NUM_THREADS=6 $MRPY spot_sheet.py --lon "$lon" --lat "$lat" --tag "$tag" --r 8 --cleanup \
    > "/tmp/log_$tag.log" 2>&1 || echo "FAIL $tag"
done
echo "SHARD $S DONE $(date)"
