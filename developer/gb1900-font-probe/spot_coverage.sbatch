#!/bin/bash
#SBATCH -M gpu
#SBATCH --partition=a100
#SBATCH --account=ishi
#SBATCH --qos=gpu-a100-n
#SBATCH --gres=gpu:4
#SBATCH -c 32
#SBATCH --mem=64G
#SBATCH -t 08:00:00
#SBATCH -J spot_cov
#SBATCH -o /vast/ishi/gb1900/probe/font/cov/spot_cov_%j.log
# Spot one batch of the full-coverage centres. The driver (coverage_driver.sh) sets CENTRES_FILE and has
# already prefetched this batch's z17 tiles onto /vast; the 4 GPU workers only READ cached tiles (compute
# nodes have no S3 route). Resume-aware: a centre whose boxes_<tag>.jsonl already exists is skipped.
set -e
cd /vast/ishi/gb1900/probe/font
CF="${CENTRES_FILE:?set CENTRES_FILE}"
MRPY=/vast/ishi/envs/mapreader/bin/python
for g in 0 1 2 3; do awk "NR%4==$g" "$CF" > /tmp/cov_$g.txt; done
worker(){ local g=$1
  while read lon lat tag; do
    [ -z "$lon" ] && continue
    bf=/vast/ishi/gb1900/edition/spot/boxes_$tag.jsonl
    if [ -f "$bf" ]; then continue; fi        # already spotted (file exists even if 0 boxes)
    CUDA_VISIBLE_DEVICES=$g OMP_NUM_THREADS=6 $MRPY spot_sheet.py --lon "$lon" --lat "$lat" --tag "$tag" --r 8 \
      > "/tmp/log_$tag.log" 2>&1 || echo "gpu$g FAIL $tag"
    rm -rf "/vast/ishi/gb1900/edition/spot/mosaics_$tag"   # spot_sheet removes PNGs; drop the empty dir
  done < /tmp/cov_$g.txt; }
for g in 0 1 2 3; do worker $g & done
wait
echo SPOTCOVDONE
