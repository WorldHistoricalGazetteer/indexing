#!/bin/bash
#SBATCH --job-name=water233-oceancmp
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/20-oceancmp-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/20-oceancmp-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=32G

# place#233 — decide between the two ocean builds ON EVIDENCE.
#
# ⚠️ POLYGON COUNT IS A BAD COMPARATOR and was my first instinct.
# water_polygons at --max-points=1000 are SPLIT PIECES, so the count measures
# geometric complexity, not coverage: a ring that fails to close can remove an
# enormous polygon while the count barely moves, and the count can shift for
# reasons purely about splitting. (indexing-5e)
#
# The three comparators that actually discriminate:
#   1. TOTAL AREA -- the coverage measure. Should be essentially unchanged.
#      A material drop is exactly the failure mode of a stricter tolerance:
#      a ring that no longer closes loses its whole polygon.
#   2. added_line SHOULD FALL 3 -> 1. Only the 28.9 m Chilean closure
#      survives ~111 m. Anything else means something other than the
#      tolerance changed and the comparison is not clean.
#   3. not_closed SHOULD RISE BY EXACTLY 2 -- Java and NSW, now refused
#      rather than spanned. More than 2 means the new constant is refusing
#      things I did not intend.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
D="${RUN_ROOT}/data"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
for f in ocean-c1deg.geojsonl ocean-c001.geojsonl; do
    [ -s "${D}/${f}" ] || { echo "FATAL: ${f} missing" >&2; exit 1; }
done

python - "${D}" <<'PY'
import json, math, os, sys, collections
D = sys.argv[1]
M = 111320.0

def ring_area_deg2(r):
    s = 0.0
    for i in range(len(r) - 1):
        s += r[i][0]*r[i+1][1] - r[i+1][0]*r[i][1]
    return abs(s) / 2.0

def feat_area_m2(g):
    t = g.get("type"); polys = g.get("coordinates") or []
    if t == "Polygon": polys = [polys]
    elif t != "MultiPolygon": return 0.0
    tot = 0.0
    for poly in polys:
        if not poly: continue
        lats = [p[1] for p in poly[0]]
        if not lats: continue
        c = math.cos(math.radians(sum(lats)/len(lats)))
        a = ring_area_deg2(poly[0])
        for h in poly[1:]:
            a -= ring_area_deg2(h)
        tot += max(a, 0.0) * (M**2) * c
    return tot

res = {}
for name, f in (("c=1deg (~111 km)", "ocean-c1deg.geojsonl"),
                ("c=0.001 (~111 m)", "ocean-c001.geojsonl")):
    n = 0; area = 0.0
    with open(os.path.join(D, f), encoding="utf-8") as fh:
        for line in fh:
            n += 1
            area += feat_area_m2(json.loads(line).get("geometry") or {})
    res[name] = (n, area)
    print(f"  {name:20s} {n:8,} polygons   {area/1e6:15,.0f} km2")

(n1, a1), (n2, a2) = res["c=1deg (~111 km)"], res["c=0.001 (~111 m)"]
print()
print(f"  polygon count delta : {n2-n1:+,} ({100*(n2-n1)/n1:+.3f}%)")
print(f"  AREA delta          : {(a2-a1)/1e6:+,.0f} km2 ({100*(a2-a1)/a1:+.4f}%)")
print()
# Earth's ocean is ~361,000,000 km2. Stated as a sourced sanity anchor, not
# as a pass criterion -- osmcoastline's water polygons are the sea as OSM
# maps it, which is not identical to that figure.
print(f"  (reference: Earth's ocean surface is ~361,000,000 km2)")
print()
if abs(a2 - a1) / a1 > 0.001:
    print("  *** AREA CHANGED BY MORE THAN 0.1%. The stricter tolerance has")
    print("  *** altered coverage, not just removed two bad bridges. Investigate")
    print("  *** before choosing -- a lost ring costs far more than a bad bridge.")
else:
    print("  Area is materially unchanged: the stricter tolerance removed the")
    print("  bridges without costing coverage.")
PY

echo
echo "=== error-table comparison ==="
conda activate whg-coastline
for db in coastline-c1deg.db coastline-c001.db; do
    echo "--- ${db} ---"
    ogrinfo -al -geom=NO "${D}/${db}" error_lines 2>/dev/null \
        | grep -E "^  error " | sort | uniq -c | sort -rn | head -8 || echo "  (unavailable)"
done
echo
echo "EXPECTED, from the tolerance change alone:"
echo "  added_line   3 -> 1   (only the 28.9 m Chilean closure survives ~111 m)"
echo "  not_closed   2 -> 4   (Java and NSW now refused rather than spanned)"
echo "Any other movement means something beyond the tolerance changed."
echo
echo "=== does the Chilean self-intersection persist? ==="
# The 28.9 m closure at -71.46685,-32.49504 is the SAME PLACE as the two GDAL
# self-intersection warnings from the first build (-71.4662,-32.4969), a
# correlation spotted across two reports hours apart (indexing-5e). The
# surviving closure is therefore not clean. If the warnings persist they are
# attributable and expected; if they vanish, something else changed.
grep -c "Self-intersection" "${RUN_ROOT}/logs/19-ocean2-"*.err 2>/dev/null \
    || echo "  (no rebuild stderr yet)"
grep -m4 "Self-intersection" "${RUN_ROOT}/logs/19-ocean2-"*.err 2>/dev/null || true
echo "=== DONE $(date -Is) ==="
