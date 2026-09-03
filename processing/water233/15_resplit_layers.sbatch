#!/bin/bash
#SBATCH --job-name=water233-resplit
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/15-resplit-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/15-resplit-%j.err
#SBATCH --time=06:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 — CLASSIFIER DEFECT: river areas are being filed as lakes.
#
# The planet split produced lakes 23,140,155 / rivers 4,856. Rivers at 0.02%
# of inland water is not credible, and the cause is ordering in classify():
#
#     if tags.get("natural") == "water":  return "lakes"     <-- fires first
#     ...
#     if waterway in {river, stream, ...}: return "rivers"
#
# `natural=water` + `water=river` is the MODERN tagging for a river area --
# it is what replaced waterway=riverbank, which is why riverbank is down to
# 3 ways planet-wide. So every river polygon on the planet took the first
# branch and landed in lakes.
#
# This is the same class again, in my own code: the branch is correct about
# what it tests and wrong about what it should catch. It also silently
# invalidated the Step 3 fallback a second time -- "drop the rivers layer"
# saves nothing when the rivers are in the lakes layer.
#
# No re-assembly needed: inland.geojsonl retains natural/water/waterway/
# landuse properties, so the split is recomputable from it.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
D="${RUN_ROOT}/data"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 30
[ -s "${D}/inland.geojsonl" ] || { echo "FATAL: inland.geojsonl missing" >&2; exit 1; }

python - "${D}" <<'PY'
import collections, json, os, sys
D = sys.argv[1]

# Water values that denote FLOWING water, i.e. a river area rather than a
# standing waterbody. Taken from the OSM water=* value list.
# Corrected after review (indexing-5e), on TAG SEMANTICS rather than
# cartographic taste. Removed as standing water, not flowing:
#   wastewater 126,188  treatment/settling basins, standing by definition
#   oxbow       54,064  a cut-off meander IS a lake: riverine in origin,
#                       standing in fact
#   moat         2,716  standing
# Kept: lock (part of the navigable waterway, and trivially small); rapids
# and stream_pool (parts of a watercourse).
# NOT changed: water=basin stays in lakes -- some basins are riverine in
# function, none in appearance, and moving 328,274 of them would be the
# bigger error. water=lagoon was never in this set, so it was already
# in lakes and needed no move.
RIVERISH = {"river", "stream", "canal", "ditch", "drain",
            "rapids", "creek", "stream_pool", "lock"}

def classify(p):
    """Corrected: consult the water=* VALUE before defaulting to lakes."""
    if p.get("waterway") == "riverbank":
        return "rivers"
    w = p.get("water")
    if w in RIVERISH:
        return "rivers"
    if p.get("natural") == "water" or w is not None:
        return "lakes"
    if p.get("landuse") == "reservoir":
        return "lakes"
    if p.get("waterway") in {"river", "stream", "canal", "ditch", "drain"}:
        return "rivers"
    return "lakes"

water_vals = collections.Counter()
old = collections.Counter()
new = collections.Counter()
moved = 0
src = os.path.join(D, "inland.geojsonl")
outs = {k: open(os.path.join(D, f"{k}.geojsonl.new"), "w", encoding="utf-8")
        for k in ("lakes", "rivers")}
with open(src, encoding="utf-8") as fh:
    for line in fh:
        f = json.loads(line)
        p = f["properties"]
        water_vals[p.get("water") or "(no water tag)"] += 1
        o = p["layer"]; n = classify(p)
        old[o] += 1; new[n] += 1
        if o != n:
            moved += 1
        p["layer"] = n
        outs[n].write(json.dumps(f, separators=(",", ":")) + "\n")
for h in outs.values():
    h.close()

print("  water=* value distribution across assembled inland areas (top 20):")
for v, n in water_vals.most_common(20):
    print(f"    {v:24s} {n:12,}")
print()
print(f"  {'layer':10s} {'before':>14s} {'after':>14s}")
for k in ("lakes", "rivers"):
    print(f"  {k:10s} {old[k]:14,} {new[k]:14,}")
print(f"\n  features reclassified: {moved:,}")
PY

echo
echo "=== swapping in the corrected split ==="
for l in lakes rivers; do
    if [ -s "${D}/${l}.geojsonl.new" ]; then
        mv -f "${D}/${l}.geojsonl.new" "${D}/${l}.geojsonl"
    else
        # An empty layer is legitimate; an ABSENT one is not the same thing.
        : > "${D}/${l}.geojsonl"
        rm -f "${D}/${l}.geojsonl.new"
        echo "NOTE: ${l} is empty after reclassification"
    fi
done
ls -la "${D}"/lakes.geojsonl "${D}"/rivers.geojsonl
echo
echo "=== per-layer source size (the Step 3 fallback needs this measured) ==="
for l in lakes rivers; do
    printf '  %-8s %12s bytes  %10s features\n' "$l" \
        "$(stat -c %s "${D}/${l}.geojsonl")" "$(wc -l < "${D}/${l}.geojsonl")"
done
report_volumes
echo "=== DONE $(date -Is) ==="
