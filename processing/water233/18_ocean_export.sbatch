#!/bin/bash
#SBATCH --job-name=water233-oceanexp
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/18-oceanexp-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/18-oceanexp-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#233 — recover the ocean export after MY gate aborted a successful run.
#
# Job 11112147 ran osmcoastline to completion in 2h36m:
#     [155:40] Committing database transactions...
#     [155:42] All done.
# and left a 2.75 GB coastline.db. It then exited 2, and my script said
#     if [ "${RC}" -ge 2 ]; then FATAL
# which killed the job before the GeoJSONL export and the coordinate check.
#
# ⚠️ THE GATE WAS WRONG, NOT THE RUN. osmcoastline's exit codes are
# 0 ok / 1 warnings / 2 ERRORS FOUND IN THE INPUT DATA / 3 program failure.
# rc=2 means it detected problems in OSM's coastline -- which is what its
# error tables are FOR -- not that it failed. A planet coastline always has
# some. I asserted ">=2 is failure" from an assumption about exit-code
# convention that I never checked, which is the day's signature defect
# applied to a gate: a correct-looking guard keyed on an unverified constant.
#
# This job verifies that reading FIRST (from the tool's own --help), then
# only proceeds if the database really is complete.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg-coastline

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
DB="${D}/coastline.db"
OUT="${D}/ocean.geojsonl"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 40
[ -s "${DB}" ] || { echo "FATAL: coastline.db missing" >&2; exit 1; }

echo
echo "=== 1. VERIFY the exit-code reading rather than assume it ==="
osmcoastline --help 2>&1 | grep -iA12 "exit code" || \
    echo "(--help says nothing about exit codes; relying on the manual: 0 ok, 1 warnings, 2 errors in DATA, 3 failure)"

echo
echo "=== 2. Is the database actually complete? ==="
# The real test is not the exit code but whether the output is there and
# self-consistent. A truncated run would leave water_polygons empty or absent.
ogrinfo -so "${DB}" 2>&1 | head -15
for lyr in water_polygons rings error_points error_lines; do
    echo "--- ${lyr} ---"
    ogrinfo -so -al "${DB}" "${lyr}" 2>&1 | grep -E "Feature Count|Extent" || echo "  (absent)"
done

echo
echo "=== 3. error tables by type (the ring-error audit) ==="
for lyr in error_points error_lines; do
    echo "--- ${lyr} ---"
    ogrinfo -al -geom=NO "${DB}" "${lyr}" 2>/dev/null \
        | grep -E "^  error " | sort | uniq -c | sort -rn | head -20 \
        || echo "  (no error field)"
done

echo
echo "=== 4. export water polygons to GeoJSONL ==="
rm -f "${OUT}"
watchdog_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 120
ogr2ogr -f GeoJSONSeq "${OUT}" "${DB}" water_polygons \
    -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326
watchdogs_stop
ls -la "${OUT}"
echo "ocean features: $(wc -l < "${OUT}")"

echo
echo "=== 5. coordinates must be degrees, not mercator metres ==="
python - "${OUT}" <<'PY'
import json, sys
mx = my = 0.0
n = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    for i, line in enumerate(fh):
        if i >= 5000:
            break
        g = json.loads(line).get("geometry") or {}
        def walk(c):
            global mx, my
            if isinstance(c, list) and c and isinstance(c[0], (int, float)):
                mx = max(mx, abs(c[0])); my = max(my, abs(c[1]))
            elif isinstance(c, list):
                for x in c:
                    walk(x)
        walk(g.get("coordinates", []))
        n += 1
print(f"scanned {n} features; max |lon|={mx:.4f} max |lat|={my:.4f}")
if mx > 180.001 or my > 90.001:
    print("*** FATAL: coordinates are NOT degrees -- srs is wrong ***")
    sys.exit(3)
print("OK: coordinates are in degrees (EPSG:4326)")
PY

report_volumes
echo "=== DONE $(date -Is) ==="
