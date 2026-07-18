#!/bin/bash
# GB-STAMP sharded cropper — CRC htc (CPU) array. Submit once the tile fetch is complete:
#   sbatch -M htc --array=0-11 processing/gb1900/crop_shard.sbatch
# One task per shard; shard k crops pins where int(pin_id,16)%12==k whose crop doesn't exist.
#SBATCH --job-name=gbcrop
#SBATCH --account=ishi
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=1-00:00:00
#SBATCH --output=/vast/ishi/gb1900/logs/gbcrop-%A_%a.out
cd /vast/ishi/elastic
/vast/ishi/envs/boundary/bin/python -m processing.gb1900.crop_shard \
  --pins   /vast/ishi/gb1900/edition/national_typed.jsonl \
  --batch-dir /vast/ishi/gb1900/edition/batches \
  --crops  /vast/ishi/gb1900/crops/national \
  --shard "$SLURM_ARRAY_TASK_ID" --nshards 12 --batch-size 4000 --zoom 16
