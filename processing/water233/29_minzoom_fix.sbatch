#!/bin/bash
#SBATCH --job-name=water236-minzoom
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/29-minzoom-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/29-minzoom-%j.err
#SBATCH --time=08:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=96G

# place#236 — confirm the cause, then fix it.
#
# ESTABLISHED SO FAR, by difference against the shipped build:
#   ocean alone, shipped flags, z0-6   -> monotonic
#   ocean alone, shipped flags, z0-10  -> monotonic
#   four layers, shipped flags, z0-10  -> troughs at z3 and z5
# The flags and the zoom range are both exonerated. The layer set is the
# only remaining difference, so the ocean is being starved of tile budget by
# the other layers.
#
# MECHANISM: 22.4 M lakes, 73.5% of them under one hectare (place#233's area
# census). At z3 one pixel is ~19.6 km; a hectare is 1/100,000th of that. The
# ponds CANNOT BE DRAWN at these zooms, but --drop-densest-as-needed does not
# know that -- it sees a dense tile, drops indiscriminately, and the
# coastline goes out with the ponds.
#
# FIX: per-feature `tippecanoe:minzoom`, set from the feature's own size, so
# a feature enters the pyramid only at the zoom where it is at least ~1 px.
# This is correct cartography independent of the bug, and it removes the
# budget pressure at its source rather than raising the budget.
#
# STEP 1 reproduces the defect with ocean+lakes at z0-6 -- a control that
# must FAIL, since my last control did not reproduce and voided its own
# experiment. STEP 2 applies the fix and must pass.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"
EXP="${D}/param-exp"
TMPDIR_TP="${SLURM_SCRATCH:-/tmp}/tippecanoe-mz"
mkdir -p "${EXP}" "${TMPDIR_TP}"
ulimit -n 65536

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 60

profile () {  # $1 = mbtiles, $2 = label
python - "$1" "$2" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print(f"\n  {sys.argv[2]}")
print(f"    {'z':<4}{'tiles':>9}{'total bytes':>15}{'vs prev':>10}{'max tile':>12}")
prev = None; troughs = []
for z, cnt, tot, mx in con.execute(
        "SELECT zoom_level, COUNT(*), SUM(LENGTH(tile_data)), MAX(LENGTH(tile_data)) "
        "FROM tiles GROUP BY zoom_level ORDER BY zoom_level"):
    r = f"{tot/prev:.2f}x" if prev else "-"
    flag = ""
    if prev and tot < prev:
        flag = "  <<TROUGH"; troughs.append(z)
    print(f"    z{z:<3}{cnt:>9,}{tot:>15,}{r:>10}{mx/1024:>10.1f}KB{flag}")
    prev = tot
print(f"    verdict: {'MONOTONIC' if not troughs else 'TROUGHS at z' + ', z'.join(map(str,troughs))}")
PY
}

echo
echo "=== STEP 1: control — ocean + lakes, shipped flags, z0-6. MUST show troughs. ==="
rm -f "${EXP}/ol-control.mbtiles"
t0=$(date +%s)
tippecanoe --output "${EXP}/ol-control.mbtiles" --name ol-control \
    --minimum-zoom=0 --maximum-zoom=6 \
    --coalesce-densest-as-needed --drop-densest-as-needed \
    --simplification=4 --detect-shared-borders \
    --temporary-directory="${TMPDIR_TP}" --force --quiet \
    -L "ocean:${D}/ocean-c001.geojsonl" \
    -L "lakes:${D}/lakes.geojsonl" 2>&1 | tail -3
echo "built in $(( $(date +%s) - t0 ))s"
profile "${EXP}/ol-control.mbtiles" "CONTROL: ocean + lakes, shipped flags, z0-6"

echo
echo "=== STEP 2: add tippecanoe:minzoom by feature size ==="
python - "${D}" <<'PY'
import json, math, os, sys, collections
D = sys.argv[1]
R0 = 156543.03392  # web-mercator m/px at z0

def extent_m(geom):
    """Longest side of the bounding box, in metres. A size the eye would
    judge by, rather than area: a long thin river is visible when a pond of
    the same area is not."""
    xs = []; ys = []
    stack = [geom.get("coordinates", [])]
    while stack:
        c = stack.pop()
        if isinstance(c, list):
            if c and isinstance(c[0], (int, float)):
                xs.append(c[0]); ys.append(c[1])
            else:
                stack.extend(c)
    if not xs:
        return 0.0, 0.0
    lat = (min(ys) + max(ys)) / 2.0
    w = (max(xs) - min(xs)) * 111320.0 * math.cos(math.radians(lat))
    h = (max(ys) - min(ys)) * 110540.0
    return max(w, h), lat

def minzoom_for(size_m, lat, maxz=10):
    if size_m <= 0:
        return maxz
    # visible when the feature spans >= 1 px: 2^z >= R0*cos(lat)/size
    res0 = R0 * math.cos(math.radians(lat))
    z = math.ceil(math.log2(max(res0 / size_m, 1.0)))
    return max(0, min(int(z), maxz))

for layer in ("lakes", "rivers", "ice"):
    src = os.path.join(D, f"{layer}.geojsonl" if layer != "ice" else "ice-only.geojsonl")
    dst = os.path.join(D, f"{layer}-mz.geojsonl")
    hist = collections.Counter(); n = 0
    with open(src, encoding="utf-8") as fh, open(dst, "w", encoding="utf-8") as out:
        for line in fh:
            f = json.loads(line)
            size, lat = extent_m(f.get("geometry") or {})
            mz = minzoom_for(size, lat)
            f["properties"]["tippecanoe:minzoom"] = mz
            hist[mz] += 1; n += 1
            out.write(json.dumps(f, separators=(",", ":")) + "\n")
    print(f"  {layer}: {n:,} features -> {dst}")
    print("    minzoom distribution: " +
          "  ".join(f"z{z}:{hist[z]:,}" for z in sorted(hist)))
    early = sum(v for z, v in hist.items() if z <= 6)
    print(f"    entering at z<=6: {early:,} ({100*early/n:.2f}%)  "
          f"— the rest cannot be seen there and no longer compete for budget")
PY

echo
echo "=== STEP 3: fixed build — ocean + lakes(minzoom), z0-6. MUST be monotonic. ==="
rm -f "${EXP}/ol-fixed.mbtiles"
t0=$(date +%s)
tippecanoe --output "${EXP}/ol-fixed.mbtiles" --name ol-fixed \
    --minimum-zoom=0 --maximum-zoom=6 \
    --coalesce-densest-as-needed --drop-densest-as-needed \
    --simplification=4 --detect-shared-borders \
    --temporary-directory="${TMPDIR_TP}" --force --quiet \
    -L "ocean:${D}/ocean-c001.geojsonl" \
    -L "lakes:${D}/lakes-mz.geojsonl" 2>&1 | tail -3
echo "built in $(( $(date +%s) - t0 ))s"
profile "${EXP}/ol-fixed.mbtiles" "FIXED: ocean + lakes with tippecanoe:minzoom, z0-6"

report_volumes
echo "=== DONE $(date -Is) ==="
