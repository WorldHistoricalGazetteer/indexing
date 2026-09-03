#!/bin/bash
#SBATCH --job-name=water233-ocean2
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/19-ocean2-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/19-ocean2-%j.err
#SBATCH --time=08:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=64G

# place#233 — rebuild the ocean with a close distance that was CHOSEN.
#
# The first build inherited --close-distance=1 while running --srs=4326, so
# the tolerance was 1 DEGREE ~= 111 km rather than the 1 metre the default
# intends in osmcoastline's native EPSG:3857. Measured consequence (job
# 11112646): of three closures, one spanned 28.9 m and two spanned
# 12.0 km and 15.6 km -- straight lines drawn across kilometres of open
# water, joining coastlines that are not connected.
#
# NEW VALUE: -c 0.001 degrees ~= 111 m. Reasoning, so it is not another
# unexamined constant:
#   * ~111 m is the scale of coastline vertex spacing and survey error, so
#     genuine hairline gaps still close (the 28.9 m one will).
#   * kilometre-scale gaps are DATA ERRORS in OSM. osmcoastline's job is to
#     report those in its error tables, not to silently span them.
#   * it is 1000x smaller than the accidental value and 100x larger than the
#     tool's default, i.e. deliberately conservative in both directions
#     rather than a faithful unit-translation that would also refuse the
#     28.9 m gap and leave one more ring open for no benefit.
#
# ⚠️ THE FIRST BUILD IS KEPT, NOT OVERWRITTEN. If refusing those two gaps
# produces a worse ocean -- unclosed rings can cost more than a bad bridge --
# we need both to compare. Choosing between them is evidence, not taste.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg-coastline

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
FILTERED="${D}/water-source.osm.pbf"
DB="${D}/coastline-c001.db"
OUT="${D}/ocean-c001.geojsonl"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 60

# Preserve the first build under an explicit name before doing anything.
[ -f "${D}/coastline.db" ] && [ ! -f "${D}/coastline-c1deg.db" ] && \
    mv "${D}/coastline.db" "${D}/coastline-c1deg.db" && echo "preserved first build as coastline-c1deg.db"
[ -f "${D}/ocean.geojsonl" ] && [ ! -f "${D}/ocean-c1deg.geojsonl" ] && \
    mv "${D}/ocean.geojsonl" "${D}/ocean-c1deg.geojsonl" && echo "preserved first export as ocean-c1deg.geojsonl"

watchdog_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 300
rm -f "${DB}"
echo
echo "=== osmcoastline, --close-distance 0.001 deg (~111 m) ==="
START=$(date +%s)
set +e
osmcoastline \
    --output-database "${DB}" \
    --overwrite \
    --output-polygons=water \
    --output-rings \
    --srs=4326 \
    --close-distance=0.001 \
    --max-points=1000 \
    --verbose \
    "${FILTERED}"
RC=$?
set -e
echo "osmcoastline rc=${RC} elapsed=$(( $(date +%s) - START ))s"
watchdogs_stop

# ⚠️ rc is a VOCABULARY, not a scale: 0 ok / 1 warnings / 2 errors found in
# the INPUT DATA / 3 program failure. rc=2 is the NORMAL outcome for a planet
# coastline and is what the error tables exist to record. The first build was
# thrown away by a gate that read ">=2" as failure. Gate on rc==3, and then
# on the CONTENTS, which is the finding rather than the process.
if [ "${RC}" -ge 3 ]; then echo "FATAL: osmcoastline program failure rc=${RC}" >&2; exit "${RC}"; fi
echo "rc=${RC} accepted (>=3 would be a program failure); verifying contents"

for lyr in water_polygons rings error_points error_lines; do
    printf '  %-16s ' "${lyr}"
    ogrinfo -so -al "${DB}" "${lyr}" 2>/dev/null | grep "Feature Count" || echo "(absent)"
done

echo
echo "=== closures in THIS build (must refuse the km-scale ones) ==="
rm -f "${D}/error_lines-c001.geojson"
ogr2ogr -f GeoJSON "${D}/error_lines-c001.geojson" "${DB}" error_lines -t_srs EPSG:4326
conda activate whg
python - "${D}/error_lines-c001.geojson" <<'PY'
import collections, json, math, sys
gj = json.load(open(sys.argv[1], encoding="utf-8"))
kinds = collections.Counter((f.get("properties") or {}).get("error")
                            for f in gj.get("features", []))
print("  error_lines by type:")
for k, v in kinds.most_common():
    print(f"      {str(k):24s} {v:6,}")
def seg_km(a, b):
    dlat = (b[1]-a[1])*111.32
    dlon = (b[0]-a[0])*111.32*math.cos(math.radians((a[1]+b[1])/2))
    return math.hypot(dlat, dlon)
added = [f for f in gj.get("features", [])
         if (f.get("properties") or {}).get("error") == "added_line"]
print(f"\n  closures made: {len(added)}")
worst = 0.0
for f in added:
    cs = f["geometry"]["coordinates"]
    if f["geometry"]["type"] == "MultiLineString":
        cs = [p for part in cs for p in part]
    d = sum(seg_km(cs[i], cs[i+1]) for i in range(len(cs)-1))
    worst = max(worst, d)
    print(f"    {d*1000:10.1f} m at {cs[0][0]:10.5f},{cs[0][1]:9.5f}")
print(f"\n  longest closure: {worst*1000:,.1f} m")
if worst > 1.0:
    print("  *** a closure still spans over 1 km -- the new tolerance did not bite")
else:
    print("  OK: no closure exceeds 1 km; the km-scale bridges are gone.")
PY

conda activate whg-coastline
echo
echo "=== export ==="
rm -f "${OUT}"
ogr2ogr -f GeoJSONSeq "${OUT}" "${DB}" water_polygons -nlt PROMOTE_TO_MULTI -t_srs EPSG:4326
ls -la "${OUT}"
echo "ocean features: $(wc -l < "${OUT}")"
echo
echo "=== COMPARISON with the first build ==="
echo "  first  (c=1 deg  ~111 km): $(wc -l < "${D}/ocean-c1deg.geojsonl") polygons"
echo "  second (c=0.001 deg ~111 m): $(wc -l < "${OUT}") polygons"
report_volumes
echo "=== DONE $(date -Is) ==="
