#!/bin/bash
#SBATCH --job-name=ipa-pprun
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH --array=0-31
#SBATCH --cpus-per-task=2
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=/vast/ishi/ipa-v8/logs/pprun-%A_%a.out
#SBATCH --error=/vast/ishi/ipa-v8/logs/pprun-%A_%a.err
# ⚠ SHARDS GO ON /ix1, NOT /vast. /vast is a 1TB allocation SHARED with
# production ES, which goes read-only at ~51GB free; it stood at 225GB with a
# restore consuming it when this was written. 67GB of feature blobs there would
# be reckless. /ix1 has 1.7TB and holds the inventory anyway.
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH=/vast/ishi/ipa-v8/code:/vast/ishi/elastic
cd /vast/ishi/ipa-v8/code
python -u -m phonetics.ipa.panphon_repop run \
  --inventory-db /ix1/ishi/data/toponyms-undscript-20260906T160000Z.compact.db \
  --out-dir /ix1/ishi/ipa-v8/shards-panphon \
  --buckets 32 \
  --bucket ${SLURM_ARRAY_TASK_ID} \
  --temp-dir /ix1/ishi/tmp/ipa-duckdb
