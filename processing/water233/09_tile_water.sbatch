#!/bin/bash
#SBATCH --job-name=water233-tile
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/09-tile-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/09-tile-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#233 Step 3 — tile to z0-10, matching osm.mbtiles.
#
# Alignment is a RELATIVE property: two layers agree at every zoom, including
# overzoomed, when they come from the same source at the same maxzoom. osm is
# z0-10, so water is built -Z0 -z10. Not a staging post.
#
# Separate layers for ocean / lakes / rivers so the style can treat them
# differently, and so the Step 3 fallback (drop rivers first) is a re-tile
# rather than a re-extraction.
#
# Tippecanoe temp goes to node-local NVMe: it is heavy RANDOM I/O and /ix1 is
# NFS, which is the one access pattern that volume is bad at.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
OUT="${D}/water.mbtiles"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-water233"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 30
ulimit -n 65536
mkdir -p "${TMPDIR_TP}"
echo "tippecanoe temp: ${TMPDIR_TP}"
df -h "${TMPDIR_TP}" | tail -1

INPUTS=()
for layer in ocean lakes rivers; do
    f="${D}/${layer}.geojsonl"
    if [ -s "$f" ]; then
        echo "  layer ${layer}: $(wc -l < "$f") features"
        INPUTS+=( -L "${layer}:${f}" )
    else
        echo "  layer ${layer}: ABSENT (${f})"
    fi
done
[ "${#INPUTS[@]}" -gt 0 ] || { echo "FATAL: no input layers" >&2; exit 1; }

echo
echo "=== tippecanoe -Z0 -z10 ==="
rm -f "${OUT}"
START=$(date +%s)
tippecanoe \
    --output "${OUT}" \
    --minimum-zoom=0 --maximum-zoom=10 \
    --coalesce-densest-as-needed \
    --drop-densest-as-needed \
    --simplification=4 \
    --detect-shared-borders \
    --no-tile-size-limit \
    --temporary-directory="${TMPDIR_TP}" \
    --force \
    "${INPUTS[@]}"
echo "tippecanoe elapsed=$(( $(date +%s) - START ))s"

echo
echo "=== STEP 3 ACCEPTANCE ==="
ls -la "${OUT}"
SIZE_BYTES=$(stat -c %s "${OUT}")
SIZE_GB=$(awk -v b="${SIZE_BYTES}" 'BEGIN{printf "%.2f", b/1073741824}')
echo "water.mbtiles = ${SIZE_GB} GB (${SIZE_BYTES} bytes)"
echo
echo "--- metadata (must read z0-10 to match osm.mbtiles) ---"
python - "${OUT}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
md = dict(con.execute("SELECT name, value FROM metadata").fetchall())
for k in ("name", "format", "minzoom", "maxzoom", "bounds", "type"):
    print(f"  {k:10s} {md.get(k)}")
print("  json/vector_layers:")
import json as j
vl = j.loads(md.get("json", "{}")).get("vector_layers", [])
for l in vl:
    print(f"    {l.get('id'):10s} minzoom={l.get('minzoom')} maxzoom={l.get('maxzoom')}")
n = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
print(f"  tiles: {n:,}")
if md.get("minzoom") != "0" or md.get("maxzoom") != "10":
    print("  *** metadata is NOT z0-10 -- does not match osm.mbtiles ***"); sys.exit(3)
print("  OK: metadata reads z0-10")
PY

echo
echo "--- size gate (issue Step 3: refuse transfer if > tileserver_free - 4GB) ---"
echo "    NOTE: this job does NOT transfer or deploy anything. The gate is"
echo "    evaluated and reported here; the deploy is out of scope and gated"
echo "    on SG and on session 78's retile campaign."
report_volumes
echo "=== DONE $(date -Is) ==="
