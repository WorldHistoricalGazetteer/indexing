#!/bin/bash
#SBATCH --job-name=ipa-ceilbal
#SBATCH --partition=htc
#SBATCH --account=ishi
#SBATCH --qos=htc-htc-s
#SBATCH --cpus-per-task=16
#SBATCH --mem=180G
#SBATCH --time=12:00:00
#SBATCH --output=/vast/ishi/ipa-v8/logs/ceilbal-%j.out
#SBATCH --error=/vast/ishi/ipa-v8/logs/ceilbal-%j.err
# ─── SIZING CHECKLIST (numbers with their justification, not remembered) ───
# threads: 16. MEASURED peak for Symphonym CPU embedding on CRC —
#     16 thr 4,430 names/s · 4 thr 2,101 · 48 thr 846 (5x WORSE than 16).
#     Run 3 x 16-thread tasks per node, never 1 x 48-thread.
# ⚠ Do NOT read a timing from a shared htc node as a thread-count effect: the
#     same 4-thread config measured 2,101/s and 382/s on two nodes — a 5.5x
#     spread that is entirely co-tenants, i.e. a confound LARGER than the
#     effect. Vary threads only inside an --exclusive allocation.
# artefacts: /ix1 (1.7 TB), NEVER /vast — /vast is a 1 TB allocation SHARED
#     with production ES, which goes READ-ONLY at ~51 GB free.
# ──────────────────────────────────────────────────────────────────────────
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH=/vast/ishi/ipa-v8/code:/vast/ishi/elastic
export OMP_NUM_THREADS=16
cd /vast/ishi/ipa-v8/code
python -u -m evaluation.recall_ceiling \
  --corpus /vast/ishi/symphonym-eval/20260905T2000Z \
  --balanced 100 \
  --out /vast/ishi/ipa-v8/logs/recall_ceiling_balanced.json
