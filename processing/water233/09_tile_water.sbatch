#!/bin/bash
#SBATCH --job-name=water233-tile
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/09-tile-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/09-tile-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=128G

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
#
# ⚠️ NO --no-tile-size-limit. It is tempting here (the ocean is enormous) and
# it is a trap in THIS repo specifically: oversize tiles are silently skipped
# by tile-join, which is the documented root cause of place#160. A tileset
# built with oversize tiles looks complete, joins without error, and is
# missing content. Let --coalesce-densest-as-needed and
# --drop-densest-as-needed shed features to fit instead: dropping small lakes
# at low zoom is correct cartography, whereas a silently skipped tile is a
# hole. The ocean itself is pre-split by osmcoastline --max-points=1000, so
# no single ocean feature should approach the limit.

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

# Ocean comes from the SECOND build. The first inherited a 1-degree
# (~111 km) close distance from --srs=4326 and bridged 12.0 km and 15.6 km
# of open water; the second used a chosen 0.001 deg (~111 m). Measured
# comparison (job 11112663): area 354,241,468 -> 354,241,475 km2, a delta of
# +7 km2 (+0.0000%), so refusing those bridges cost no coverage at all.
declare -A SRC=( [ocean]="ocean-c001.geojsonl" [lakes]="lakes.geojsonl" [rivers]="rivers.geojsonl" )
INPUTS=()
for layer in ocean lakes rivers; do
    f="${D}/${SRC[$layer]}"
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

# Measure the largest tile rather than assume none is oversize. 500 KB is
# tippecanoe's default per-tile ceiling and the threshold at which tile-join
# silently skips a tile (place#160). Reported per zoom so a single fat zoom
# level is visible rather than averaged away.
print("  largest tile per zoom:")
rows = con.execute(
    "SELECT zoom_level, COUNT(*), MAX(LENGTH(tile_data)), SUM(LENGTH(tile_data)) "
    "FROM tiles GROUP BY zoom_level ORDER BY zoom_level").fetchall()
worst = 0
for z, cnt, mx, tot in rows:
    worst = max(worst, mx)
    flag = "  <-- OVER 500 KB" if mx > 500 * 1024 else ""
    print(f"    z{z:<3} tiles={cnt:>8,}  max={mx/1024:9.1f} KB  "
          f"total={tot/1048576:8.1f} MB{flag}")
print(f"  worst tile overall: {worst/1024:.1f} KB")
if worst > 500 * 1024:
    print("  *** at least one tile exceeds 500 KB: tile-join would SKIP it")
    print("  *** silently (place#160). Investigate before any deploy.")
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
