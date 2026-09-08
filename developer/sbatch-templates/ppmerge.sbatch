#!/bin/bash
#SBATCH --job-name=ipa-ppmerge
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH --cpus-per-task=16
#SBATCH --mem=180G
#SBATCH --time=12:00:00
#SBATCH --output=/vast/ishi/ipa-v8/logs/ppmerge-%j.out
#SBATCH --error=/vast/ishi/ipa-v8/logs/ppmerge-%j.err
# ─── SIZING CHECKLIST (numbers with their justification, not remembered) ───
# threads: 16. MEASURED peak for Symphonym CPU embedding on CRC —
#     16 thr 4,430 names/s · 4 thr 2,101 · 48 thr 846 (5x WORSE than 16).
# ⚠ Do NOT read a timing from a shared htc node as a thread-count effect: the
#     same 4-thread config measured 2,101/s and 382/s on two nodes — a
#     confound LARGER than the effect.
# artefacts: /ix1 (1.7 TB), NEVER /vast — /vast is a 1 TB allocation SHARED
#     with production ES, which goes READ-ONLY at ~51 GB free.
# 🛑 This job COPIES the 121 GB inventory and rebuilds `toponyms` on the COPY.
#     The original is never opened for writing. It does NOT swap — it leaves a
#     verified candidate and stops, so a human looks before the rename.
# ──────────────────────────────────────────────────────────────────────────
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH=/vast/ishi/ipa-v8/code:/vast/ishi/elastic
cd /vast/ishi/ipa-v8/code
python -u -m phonetics.ipa.panphon_repop merge \
  --inventory-db /ix1/ishi/data/toponyms-undscript-20260906T160000Z.compact.db \
  --shard-glob '/ix1/ishi/ipa-v8/shards-panphon/features.*.parquet' \
  --buckets 32 \
  --work-db /ix1/ishi/data/toponyms-panphon-CANDIDATE.db \
  --temp-dir /ix1/ishi/tmp/ipa-duckdb \
  --max-temp 200GB \
  --execute
