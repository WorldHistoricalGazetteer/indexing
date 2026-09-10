#!/bin/bash
# ============================================================================
# RE-EXTRACT, JOB 2 of 2 — corpus rebuild with corrected IPA
# ============================================================================
# Run ONLY after job 1 has produced this RUNID's neural_phonetics.parquet.
#
# Paths are on /ix1 DELIBERATELY, and this reverses an earlier instinct:
# `temp_db_path` is created under --scratch-dir and is node-local throughout
# (rebuild_toponyms_index.py:2269-2271); --db-path receives only a sequential
# shutil.copy2 of the FINISHED file. So the write-performance argument that
# once put it on /vast never applied to that path — and /vast is the 1 TB
# volume shared with production Elasticsearch, which this pipeline took
# write-blocked twice on 7 Sep. On /ix1 the rebuild touches /vast not at all.
#
# --db-path is RUN-SCOPED and must not already exist. `--resume` copies
# whatever is at that path to scratch and SKIPS extraction entirely
# (:2275-2278), so a path holding a PREVIOUS corpus would be silently adopted
# and the run would report success on stale data.
# ============================================================================
#SBATCH --job-name=whg-reextract-rebuild
#SBATCH --output=/vast/ishi/elastic/logs/reextract_rebuild_%j.out
#SBATCH --error=/vast/ishi/elastic/logs/reextract_rebuild_%j.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G
#SBATCH --account=ishi

set +u
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
set -euo pipefail
cd /vast/ishi/elastic

RUNID="${RUNID:?set RUNID, e.g. ipafix-20260908T140000Z}"
IDX="toponyms_$(echo "$RUNID" | tr '[:upper:]' '[:lower:]')"
DB=/ix1/ishi/data/toponyms-${RUNID}.db
OUTDIR=/ix1/ishi/models/phonetic/data/v${RUNID}
NEURAL=${OUTDIR}/neural_phonetics.parquet

echo "=== PREFLIGHT — every one of these is a defect we hit this week ==="
# 1. Charsiu bound present (2e59f37): unbounded ByT5 truncates IPA at ~15 chars
grep -q "CHARSIU_MAX_NEW_TOKENS" phonetics/extraction/rebuild_toponyms_index.py \
  || { echo "ABORT: rebuild lacks the Charsiu token bound"; exit 1; }
# 2. corrected Charsiu tags (c37d927 + a910e8e): `cmn` emits Japanese for Han
python -c "
from phonetics.extraction.precompute_neural_phonetics import CHARSIU_LANG_MAP as M
assert M.get('zh') == 'zho-s', f'ABORT: lang map is {M.get(\"zh\")!r}, not zho-s'
"
# 3. CHECKPOINT before both promoting copies (5e66a43): else 198 GB holds 121 GB
[ "$(grep -c 'conn.execute("CHECKPOINT")' phonetics/extraction/rebuild_toponyms_index.py)" -ge 2 ] \
  || { echo "ABORT: fewer than two CHECKPOINT calls"; exit 1; }
# 4. ⚠ THIS RUNID's neural phonetics, checked BY ROW COUNT not by size.
#    `[ -s ]` tests non-zero bytes, and an EMPTY parquet is not zero bytes: it
#    carries PAR1 magic at both ends plus footer metadata, so a 0-row file is a
#    few hundred bytes and passes. Job 1 producing an empty parquet — which is
#    what happens if its source inventory is missing — would then sail through
#    here, Priority 2 would supply nothing, Priority 3 would skip
#    zh/ko/yue/gan/wuu entirely, and both jobs would exit 0:0.
[ -f "$NEURAL" ] || { echo "ABORT: $NEURAL missing — run job 1 first"; exit 1; }
# ⚠ THE LANGUAGE IS IN `toponym_id`, NOT A COLUMN. precompute emits exactly
# (toponym_id, ipa, panphon_features, panphon_embedding) — ids are `name@lang`,
# e.g. `山尾山@zh`. Both templates originally filtered `where lang='zh'`, which
# is a BinderException, so job 1's verification FAILED THE JOB after five hours
# of successful GPU work, and job 2 would have aborted at preflight the same way.
# The expectation was written from an assumed schema instead of the artefact.
python - "$NEURAL" <<'PY'
import sys, duckdb
con = duckdb.connect()
n = con.execute(f"select count(*) from read_parquet('{sys.argv[1]}')").fetchone()[0]
zh = con.execute(f"select count(*) from read_parquet('{sys.argv[1]}') "
                 f"where toponym_id like '%@zh' and ipa is not null").fetchone()[0]
print(f"  neural parquet: {n:,} rows, {zh:,} zh with ipa")
assert n  > 1_000_000, f"ABORT: parquet holds only {n:,} rows — job 1 produced an empty or partial file"
assert zh >   500_000, f"ABORT: only {zh:,} zh rows with ipa — the languages this run exists to fix are missing"
PY
# 5. run-scoped DB must NOT exist, or --resume can adopt a previous corpus
[ -e "$DB" ] && { echo "ABORT: $DB exists; --resume would adopt it"; exit 1; }
# 6. staging ES must be up and must not already hold the target index
: "${ES_URL:?source /ix1/ishi/esinfo/es-staging.env first}"
curl -sf -m 20 "$ES_URL/_cluster/health" >/dev/null \
  || { echo "ABORT: staging ES unreachable at $ES_URL"; exit 1; }
# 7. free space, asserted rather than hoped (#255)
AVAIL=$(df --output=avail -BG /ix1/ishi | tail -1 | tr -dc 0-9)
[ "$AVAIL" -lt 400 ] && { echo "ABORT: /ix1 ${AVAIL}G free, need ~400G"; exit 1; }
SCR=$(df --output=avail -BG "/scratch" 2>/dev/null | tail -1 | tr -dc 0-9 || echo 0)
[ -n "$SCR" ] && [ "$SCR" -lt 300 ] && { echo "ABORT: /scratch ${SCR}G free, working DB needs ~200G"; exit 1; }
echo "  all preflight checks passed (/ix1 ${AVAIL}G, /scratch ${SCR}G)"

mkdir -p "$OUTDIR" "/scratch/slurm-${SLURM_JOB_ID}"
echo "=== rebuild ${RUNID} on $(hostname) at $(date -Is) ==="
echo "  target index : $IDX   (new dated concrete, never the alias)"
echo "  db-path      : $DB"
echo "  neural       : $NEURAL"

python -u -m phonetics.extraction.rebuild_toponyms_index \
  --es-host "$ES_URL" \
  --toponyms-index "$IDX" \
  --namespaces gn osm wd tgn pl gb nl ukhc dp iv un ohm chgis tm ofs clio whg \
               hgis po og dgsd alc kain_par vob_rd vob_rc vob_cty vob_lgd \
  --training-namespaces gn wd tgn \
  --db-path "$DB" \
  --output-dir "$OUTDIR" \
  --precomputed-phonetics "$NEURAL" \
  --scratch-dir "/scratch/slurm-${SLURM_JOB_ID}" \
  --num-workers 14 \
  --confirm

echo "=== finished $(date -Is) ==="
