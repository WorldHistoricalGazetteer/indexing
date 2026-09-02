#!/bin/bash
#SBATCH --job-name=water233-mpcontrol
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/06-mpcontrol-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/06-mpcontrol-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=32G

# place#233 Step 0d + CONTROL 2 — do multipolygon RELATIONS survive assembly?
#
# ⚠️ This is a PRE-CONDITION. It runs on a small regional extract and must
# pass BEFORE the planet run, because its failure mode is invisible in a
# total: natural=water is dominated numerically by simple closed-way ponds,
# so an assembler that drops every relation still emits a large, healthy
# polygon count while losing precisely the big complex lakes with islands.
#
#   polygons ~ ways + relations  -> assembly works
#   polygons ~ ways only         -> relations silently dropped
#
# Region: the Finnish Lakeland (Saimaa), chosen because it is dense in large
# island-bearing multipolygon lakes, so the control has something to detect.
# The positive control is not asserted in advance -- the script REPORTS the
# largest relation-derived lakes it found, so the named example comes from
# the data rather than from my assumption about what is there.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
OSMIUM="env LD_LIBRARY_PATH=$CONDA_PREFIX/lib osmium"

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
FILTERED="${RUN_ROOT}/data/water-source.osm.pbf"
EXTRACT="${RUN_ROOT}/data/control-saimaa.osm.pbf"
BBOX="${BBOX:-27.0,61.0,29.5,62.3}"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 5
[ -s "${FILTERED}" ] || { echo "FATAL: filtered PBF missing or empty" >&2; exit 1; }

echo
echo "=== [0d] cutting regional extract  bbox=${BBOX} ==="
# -s smart keeps multipolygon relations complete, which is the whole point
# here: a strategy that clipped relations would fake the failure we are
# testing for.
${OSMIUM} extract --bbox "${BBOX}" -s smart \
    --output "${EXTRACT}" --overwrite "${FILTERED}"
ls -la "${EXTRACT}"

echo
echo "=== INPUT counts on the extract (ways vs relations, separately) ==="
python "${RUN_ROOT}/scripts/count_water.py" "${EXTRACT}" \
    --out "${RUN_ROOT}/data/counts.control.json"

echo
echo "=== assembling areas from the extract ==="
python "${RUN_ROOT}/scripts/assemble_inland_water.py" "${EXTRACT}" \
    --out "${RUN_ROOT}/data/control-saimaa.geojsonl" \
    --stats-out "${RUN_ROOT}/data/assembly.control.json" \
    --progress-every 20000
ASSEMBLY_RC=$?
echo "assembler rc=${ASSEMBLY_RC}"

echo
echo "=== CONTROL 2 VERDICT ==="
python - "${RUN_ROOT}/data/counts.control.json" "${RUN_ROOT}/data/assembly.control.json" \
        "${RUN_ROOT}/data/control-saimaa.geojsonl" <<'PY'
import json, sys, collections
counts = json.load(open(sys.argv[1]))["counts"]
asm    = json.load(open(sys.argv[2]))

in_closed_ways = counts["natural=water"]["way_closed"]
in_relations   = counts["natural=water"]["relation"]
in_mp          = counts["natural=water"]["relation_multipolygon"]
out_way        = asm["from_way"]
out_rel        = asm["from_relation"]
out_total      = asm["areas_written"]

print(f"  natural=water closed ways IN : {in_closed_ways:,}")
print(f"  natural=water relations   IN : {in_relations:,}  (multipolygon {in_mp:,})")
print(f"  areas from ways          OUT : {out_way:,}")
print(f"  areas from relations     OUT : {out_rel:,}")
print(f"  total areas written      OUT : {out_total:,}")
print()

if out_rel == 0 and in_mp > 0:
    print(f"  *** CONTROL 2 FAILED ***")
    print(f"  {in_mp:,} multipolygon relations went in and ZERO areas came out of")
    print(f"  relations. Assembly is dropping multipolygons. Note the total")
    print(f"  ({out_total:,}) still looks perfectly healthy -- which is exactly")
    print(f"  why the split is the control and the total is not.")
    sys.exit(3)

# Name the largest relation-derived lakes actually found, as the positive
# control -- taken from the data, not assumed in advance.
best = []
with open(sys.argv[3], encoding="utf-8") as fh:
    for line in fh:
        f = json.loads(line)
        g = f["geometry"]
        n = sum(len(r) for poly in g.get("coordinates", []) for r in poly) \
            if g.get("type") == "MultiPolygon" else 0
        rings = sum(len(poly) for poly in g.get("coordinates", [])) \
            if g.get("type") == "MultiPolygon" else 0
        name = f["properties"].get("name")
        if name and rings > 1:
            best.append((rings, n, name))
best.sort(reverse=True)
print("  largest island-bearing waterbodies assembled (rings > 1 means holes/islands):")
for rings, verts, name in best[:10]:
    print(f"    {name[:42]:44s} rings={rings:5,}  vertices={verts:8,}")
if not best:
    print("    (none found with rings > 1 -- weak positive control, investigate)")
    sys.exit(4)
print()
print(f"  CONTROL 2 PASSED: {out_rel:,} areas assembled from relations, and")
print(f"  {len(best):,} multi-ring waterbodies (islands/holes) were built.")
PY

report_volumes
echo "=== DONE $(date -Is) ==="
