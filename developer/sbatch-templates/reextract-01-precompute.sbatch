#!/bin/bash
# ============================================================================
# RE-EXTRACT, JOB 1 of 2 — regenerate neural phonetics with the CORRECTED tags
# ============================================================================
# ⚠ NOT OPTIONAL, AND MUST PRECEDE THE REBUILD.
#
# The rebuild consumes `neural_phonetics.parquet` as Priority 2 for
# zh/ko/yue/gan/wuu. The existing file (2026-02-19) was built when precompute's
# own CHARSIU_LANG_MAP said {'zh':'cmn', ...}, and `cmn` is not a tag
# CharsiuG2P knows — for Han input it silently emits Japanese on'yomi. Reusing
# it re-imports the exact defect the re-extract exists to fix; omitting it makes
# the rebuild SKIP those languages (Priority 3). So it is regenerated here.
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
#SBATCH --cpus-per-task=16
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
NAMESPACES="gn osm wd tgn pl gb nl ukhc dp iv un ohm chgis tm ofs clio whg hgis po og dgsd alc kain_par vob_rd vob_rc vob_cty vob_lgd"

echo "=== PREFLIGHT ==="
# 1. the tree must carry the fixes, or this job regenerates the same defect
grep -q "zho-s" phonetics/ipa/routes.py \
  || { echo "ABORT: routes.py lacks the zho-s fix (c37d927)"; exit 1; }
python -c "
from phonetics.extraction.precompute_neural_phonetics import CHARSIU_LANG_MAP as M
assert M.get('zh') == 'zho-s', f'ABORT: precompute map still {M.get(\"zh\")!r}'
print('  lang map OK:', M)
"
# 2. ⚠ SRC_DB must EXIST AND HOLD ROWS. A read-write DuckDB attach to a missing
#    path CREATES an empty database there, so the accessor that tests the file
#    is the accessor that manufactures it. Probe READ-ONLY and require content.
[ -f "$SRC_DB" ] || { echo "ABORT: source inventory absent: $SRC_DB"; exit 1; }
python - "$SRC_DB" "$NAMESPACES" <<'PY'
import sys, duckdb
db, wanted = sys.argv[1], set(sys.argv[2].split())
con = duckdb.connect(db, read_only=True)          # read_only: errors, never creates
n = con.execute("select count(*) from toponyms").fetchone()[0]
print(f"  source inventory: {n:,} toponyms")
assert n > 50_000_000, f"ABORT: source holds only {n:,} rows — wrong or truncated file"
# D. namespace coverage: a namespace absent here gets no neural phonetics and
#    then vanishes down Priority 3 in job 2, silently.
have = {r[0] for r in con.execute("select distinct namespace from toponym_namespaces").fetchall()}
missing = sorted(wanted - have)
print(f"  namespaces in source: {len(have)}; job 2 will request {len(wanted)}")
if missing:
    print("  ⚠ NOT IN SOURCE (these get no neural phonetics):", ", ".join(missing))
PY
# 3. never overwrite an existing parquet — a stale one must not be adopted
[ -e "$OUT" ] && { echo "ABORT: $OUT exists; use a fresh RUNID"; exit 1; }
# 4. free space on the target volume (#255: assert, do not hope)
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

echo "=== POSITIVE CONTROL ON THE OUTPUT ==="
# ⚠ The preflight verified the MAP — i.e. what we ASKED CharsiuG2P for. This
# verifies what it RETURNED. A model that ignored or fell back on the `zho-s`
# tag would pass every check above. The map was wrong for seven months while
# every stage reported success, so "the tag is right" is not the property that
# matters; "the output is Mandarin" is.
# ⚠ THE LANGUAGE IS IN `toponym_id`, NOT A COLUMN. precompute emits exactly
# (toponym_id, ipa, panphon_features, panphon_embedding) — ids are `name@lang`,
# e.g. `山尾山@zh`. Both templates originally filtered `where lang='zh'`, which
# is a BinderException, so job 1's verification FAILED THE JOB after five hours
# of successful GPU work, and job 2 would have aborted at preflight the same way.
# The expectation was written from an assumed schema instead of the artefact.
python - "$OUT" <<'PY'
import sys, duckdb
con = duckdb.connect()
q = (f"select ipa from read_parquet('{sys.argv[1]}') "
     f"where toponym_id like '%@zh' and ipa is not null")
rows = [r[0] for r in con.execute(q).fetchall()]
assert rows, "ABORT: parquet contains no zh rows at all"
JA = ("ɯ", "ɴ")                       # Japanese-only phonemes
TONE = ("˥", "˦", "˧", "˨", "˩")      # Mandarin tone letters
ja  = sum(any(c in s for c in JA)   for s in rows)
tone = sum(any(c in s for c in TONE) for s in rows)
print(f"  zh rows: {len(rows):,}")
print(f"  carrying Japanese-only phonemes (ɯ/ɴ): {ja:,}  ({100*ja/len(rows):.1f}%)  — was 55.7% under `cmn`")
print(f"  carrying Mandarin tone letters:        {tone:,}  ({100*tone/len(rows):.1f}%)")
assert 100*ja/len(rows)   < 10.0, f"ABORT: {100*ja/len(rows):.1f}% Japanese phonemes — the `cmn` defect is back"
assert 100*tone/len(rows) > 50.0, f"ABORT: only {100*tone/len(rows):.1f}% carry tone letters — output is not Mandarin"
print("  POSITIVE CONTROL PASSED — output is Mandarin, not Japanese")
PY

echo "=== finished $(date -Is) ==="
ls -la "$OUT"
