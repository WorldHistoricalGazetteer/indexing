#!/bin/bash
# ============================================================================
# RE-EXTRACT, JOB 1 of 2 — regenerate neural phonetics with the CORRECTED tags
# ============================================================================
# ⚠ THIS JOB IS NOT OPTIONAL AND MUST PRECEDE THE REBUILD.
#
# The rebuild consumes `neural_phonetics.parquet` as Priority 2 for zh/ko/yue/
# gan/wuu. The existing file (2026-02-19) was built when precompute's own
# CHARSIU_LANG_MAP said {'zh':'cmn', ...}, and `cmn` is not a tag CharsiuG2P
# knows — for Han input it silently emits Japanese on'yomi. Reusing it
# re-imports the exact defect the re-extract exists to fix; omitting it makes
# the rebuild SKIP those languages entirely (Priority 3). So it is regenerated.
#
#   北京   <cmn>   -> hokːjoɯ         (Japanese)
#          <zho-s> -> peɪ˨˩˦tɕɪŋ˥˥    (correct Mandarin)
# ============================================================================
#SBATCH --job-name=whg-reextract-precompute
#SBATCH --output=/vast/ishi/elastic/logs/reextract_precompute_%j.out
#SBATCH --error=/vast/ishi/elastic/logs/reextract_precompute_%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=l40s
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --account=ishi

set +u
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
set -euo pipefail
cd /vast/ishi/elastic

RUNID="${RUNID:?set RUNID, e.g. ipafix-20260908T140000Z}"
SRC_DB=/ix1/ishi/data/toponyms-undscript-20260906T160000Z.compact.db
OUT=/ix1/ishi/models/phonetic/data/v${RUNID}/neural_phonetics.parquet

echo "=== PREFLIGHT ==="
# 1. the tree must carry the fixes, or this job regenerates the same defect
grep -q "zho-s" phonetics/ipa/routes.py \
  || { echo "ABORT: routes.py lacks the zho-s fix (c37d927)"; exit 1; }
python -c "
from phonetics.extraction.precompute_neural_phonetics import CHARSIU_LANG_MAP as M
assert M.get('zh') == 'zho-s', f'ABORT: precompute map still {M.get(\"zh\")!r}'
print('  lang map OK:', M)
"
# 2. never overwrite an existing parquet — a stale one must not be adopted
[ -e "$OUT" ] && { echo "ABORT: $OUT exists; use a fresh RUNID"; exit 1; }
# 3. free space on the target volume (#255: assert, do not hope)
AVAIL=$(df --output=avail -BG /ix1/ishi | tail -1 | tr -dc 0-9)
[ "$AVAIL" -lt 100 ] && { echo "ABORT: /ix1 only ${AVAIL}G free"; exit 1; }
echo "  /ix1 ${AVAIL}G free, output path clear"

mkdir -p "$(dirname "$OUT")"
echo "=== precompute on $(hostname) at $(date -Is) ==="
nvidia-smi --query-gpu=name --format=csv,noheader || true

python -u -m phonetics.extraction.precompute_neural_phonetics \
  --db-path "$SRC_DB" \
  --output "$OUT" \
  --training-namespaces gn wd tgn \
  --scratch-dir "/scratch/slurm-${SLURM_JOB_ID}" \
  --device cuda

echo "=== finished $(date -Is) ==="
ls -la "$OUT"
