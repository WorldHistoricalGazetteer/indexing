#!/bin/bash
#SBATCH --job-name=water233-areadist
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/16-areadist-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/16-areadist-%j.err
#SBATCH --time=08:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 — what does a MINIMUM-AREA threshold buy?
#
# The Step 3 fallback ladder reaches for levers by LAYER, and both rungs
# proposed so far turned out to be empty: waterway=riverbank is 3 ways
# planet-wide, and "drop the rivers layer" saves nothing while river areas
# are misfiled as lakes. The natural axis is SIZE, not layer (indexing-5e).
#
# At z10 one pixel is ~153 m at the equator:
#     1 ha pond    ~100 m across   ~0.65 px   invisible
#    10 ha lake    ~316 m across   ~2.1 px    barely a mark
#   100 ha lake     ~1 km across   ~6.5 px    visible
#
# So a minimum-area threshold is orthogonal to the acceptance criterion (the
# coast is untouched, large lakes are untouched) and is cartographically
# correct rather than a degradation, unlike capping zoom which makes
# everything worse. Whether it is a BIG lever depends entirely on how the
# 23.1 M features are distributed by area, which nobody has measured.
#
# ⚠️ No threshold is applied here and none is recommended. This job only
# reports the distribution. Two guesses about where river area lives have
# already been wrong; the number decides.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 1

python - "${RUN_ROOT}/data/inland.geojsonl" "${RUN_ROOT}/data/area_distribution.json" <<'PY'
import json, math, sys, collections

src, out = sys.argv[1], sys.argv[2]

# Thresholds in hectares (1 ha = 10,000 m2).
BUCKETS = [0.1, 1, 5, 10, 50, 100, 1000]

def ring_area_deg2(ring):
    """Planar shoelace in degrees^2."""
    s = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0

M_PER_DEG = 111320.0

def feature_area_m2(geom):
    """Outer rings minus holes, scaled by cos(lat) for longitude convergence.
    Good to a few percent at hectare scale, which is all a bucketing needs."""
    t = geom.get("type")
    polys = geom.get("coordinates") or []
    if t == "Polygon":
        polys = [polys]
    elif t != "MultiPolygon":
        return 0.0
    total = 0.0
    for poly in polys:
        if not poly:
            continue
        lats = [pt[1] for pt in poly[0]]
        if not lats:
            continue
        clat = math.cos(math.radians(sum(lats) / len(lats)))
        a = ring_area_deg2(poly[0])
        for hole in poly[1:]:
            a -= ring_area_deg2(hole)
        total += max(a, 0.0) * (M_PER_DEG ** 2) * clat
    return total

n = 0
areas_bytes = collections.defaultdict(int)
areas_count = collections.defaultdict(int)
by_layer = collections.defaultdict(lambda: [0, 0])
total_bytes = 0
zero_area = 0

with open(src, encoding="utf-8") as fh:
    for line in fh:
        n += 1
        if n % 2_000_000 == 0:
            print(f"  ... {n:,} features", file=sys.stderr, flush=True)
        b = len(line)
        total_bytes += b
        f = json.loads(line)
        a_ha = feature_area_m2(f["geometry"]) / 10_000.0
        if a_ha <= 0:
            zero_area += 1
        layer = f["properties"].get("layer", "?")
        by_layer[layer][0] += 1
        by_layer[layer][1] += b
        for t in BUCKETS:
            if a_ha < t:
                areas_count[t] += 1
                areas_bytes[t] += b

res = {
    "features": n, "total_bytes": total_bytes, "zero_area": zero_area,
    "below": {str(t): {"count": areas_count[t], "bytes": areas_bytes[t]}
              for t in BUCKETS},
    "by_layer": {k: {"count": v[0], "bytes": v[1]} for k, v in by_layer.items()},
}
with open(out, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(res, indent=2) + "\n")

print(f"\n  total features : {n:,}")
print(f"  total bytes    : {total_bytes:,} ({total_bytes/2**30:.2f} GiB)")
print(f"  zero/degenerate area: {zero_area:,}")
print()
print(f"  {'drop below':>12s} {'features cut':>14s} {'% of count':>11s} "
      f"{'bytes cut':>16s} {'% of bytes':>11s} {'approx px @ z10':>16s}")
for t in BUCKETS:
    side_m = math.sqrt(t * 10_000)
    px = side_m / 152.9
    print(f"  {t:>9.1f} ha {areas_count[t]:14,} {100*areas_count[t]/n:10.2f}% "
          f"{areas_bytes[t]:16,} {100*areas_bytes[t]/total_bytes:10.2f}% "
          f"{px:14.2f} px")
print()
print("  by layer:")
for k, v in sorted(by_layer.items()):
    print(f"    {k:8s} {v[0]:12,} features  {v[1]:16,} bytes "
          f"({v[1]/2**30:.2f} GiB)")
print()
print("  NOTE: areas are cos(lat)-scaled planar approximations, good to a few")
print("  percent at hectare scale. No threshold applied; distribution only.")
PY
report_volumes
echo "=== DONE $(date -Is) ==="
