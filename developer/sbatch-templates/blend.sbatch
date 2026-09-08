#!/bin/bash
#SBATCH --job-name=ipa-blend
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH --cpus-per-task=16
#SBATCH --mem=180G
#SBATCH --time=12:00:00
#SBATCH --output=/vast/ishi/ipa-v8/logs/blend-%j.out
#SBATCH --error=/vast/ishi/ipa-v8/logs/blend-%j.err
# threads: 16 — MEASURED peak for Symphonym CPU embedding (16 thr 4,430 names/s;
#   48 thr 846, 5x worse). ⚠ Do not read a shared-node timing as a thread effect:
#   the same 4-thread config measured 2,101/s and 382/s on two nodes.
# artefacts: /ix1 — /vast is shared with production ES (read-only at ~51GB free).
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH=/vast/ishi/ipa-v8/code:/vast/ishi/elastic
export OMP_NUM_THREADS=16
cd /vast/ishi/ipa-v8/code
python -u -m evaluation.blend_realisation \
  --corpus /vast/ishi/symphonym-eval/20260905T2000Z \
  --balanced 100 \
  --out /vast/ishi/ipa-v8/logs/blend_realisation.json
