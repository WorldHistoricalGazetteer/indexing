#!/bin/bash
#SBATCH --job-name=water233-ocean
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/07-ocean-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/07-ocean-%j.err
#SBATCH --time=08:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#233 Step 1 — OCEAN, via osmcoastline.
#
# natural=coastline is carried on OPEN way fragments that must be stitched
# into closed rings planet-wide, with antimeridian and pole handling. That is
# what osmcoastline does and why it had to be built from source.
#
# ⚠️ --srs=4326. osmcoastline DEFAULTS TO EPSG:3857 (web mercator metres).
# Tippecanoe wants lon/lat degrees. Getting this wrong produces a valid
# database full of coordinates in the millions that tiles to an empty or
# wildly displaced world -- a silent, plausible-looking failure.
#
# Runs in the whg-coastline env, NOT whg: osmcoastline is linked against
# conda's GDAL/GEOS, and that env's own ogr2ogr is internally consistent with
# it (the SYSTEM ogr2ogr breaks under conda libs, and conda libs are what this
# binary needs).

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg-coastline

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
FILTERED="${RUN_ROOT}/data/water-source.osm.pbf"
DB="${RUN_ROOT}/data/coastline.db"
OUT_JSONL="${RUN_ROOT}/data/ocean.geojsonl"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 60
[ -s "${FILTERED}" ] || { echo "FATAL: filtered PBF missing" >&2; exit 1; }

echo "--- toolchain in this env ---"
osmcoastline --version 2>&1 | head -2
command -v ogr2ogr && ogr2ogr --version || { echo "FATAL: no conda ogr2ogr in whg-coastline" >&2; exit 1; }

watchdog_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 120
rm -f "${DB}"

echo
echo "=== running osmcoastline (water polygons, EPSG:4326) ==="
START=$(date +%s)
set +e
osmcoastline \
    --output-database "${DB}" \
    --overwrite \
    --output-polygons=water \
    --output-rings \
    --srs=4326 \
    --max-points=1000 \
    --verbose \
    "${FILTERED}"
RC=$?
set -e
echo "osmcoastline rc=${RC} elapsed=$(( $(date +%s) - START ))s"
watchdogs_stop
# rc 1 = warnings only (some coastline errors are normal on a planet);
# rc >=2 is a real failure.
if [ "${RC}" -ge 2 ]; then echo "FATAL: osmcoastline failed rc=${RC}" >&2; exit "${RC}"; fi
[ "${RC}" -eq 1 ] && echo "NOTE: rc=1 means warnings (coastline errors); continuing, see counts below"

ls -la "${DB}"
echo
echo "=== what did it actually produce? (denominators) ==="
ogrinfo -so -al "${DB}" 2>&1 | grep -E "^Layer name|^Feature Count|^Geometry:|Extent" || true

echo
echo "=== exporting water polygons to GeoJSONL ==="
rm -f "${OUT_JSONL}"
ogr2ogr -f GeoJSONSeq "${OUT_JSONL}" "${DB}" water_polygons \
    -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326
ls -la "${OUT_JSONL}"

echo
echo "=== sanity: coordinates must be degrees, not mercator metres ==="
head -c 400 "${OUT_JSONL}"; echo
python - "${OUT_JSONL}" <<'PY'
import json, sys
mx = my = 0.0
n = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    for i, line in enumerate(fh):
        if i >= 2000: break
        g = json.loads(line).get("geometry") or {}
        def walk(c):
            global mx, my
            if isinstance(c, list) and c and isinstance(c[0], (int, float)):
                mx = max(mx, abs(c[0])); my = max(my, abs(c[1]))
            elif isinstance(c, list):
                for x in c: walk(x)
        walk(g.get("coordinates", [])); n += 1
print(f"scanned {n} features; max |lon|={mx:.4f} max |lat|={my:.4f}")
if mx > 180.001 or my > 90.001:
    print("*** FATAL: coordinates are NOT degrees -- srs is wrong ***"); sys.exit(3)
print("OK: coordinates are in degrees (EPSG:4326)")
PY

echo
echo "=== ocean feature count ==="
wc -l < "${OUT_JSONL}"
report_volumes
echo "=== DONE $(date -Is) ==="
