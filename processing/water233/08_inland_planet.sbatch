#!/bin/bash
#SBATCH --job-name=water233-inland
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/08-inland-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/08-inland-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=192G

# place#233 Step 2 — INLAND WATER over the whole filtered extract.
#
# ⚠️ DO NOT RUN THIS BEFORE CONTROL 2 (job 06) HAS PASSED. Its whole purpose
# is to establish, on a small extract in minutes, that multipolygon relations
# survive assembly -- because at planet scale the failure is invisible in the
# output size and the polygon total.
#
# Memory is generous because area assembly holds a node-location index for
# every node referenced by every candidate way. That index is why Step 0c
# (tags-filter) exists: against the raw planet this would not fit.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
FILTERED="${RUN_ROOT}/data/water-source.osm.pbf"
ALL="${RUN_ROOT}/data/inland.geojsonl"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 120

# Refuse to run if control 2 has not passed. The scheduler dependency also
# enforces this, but a human resubmitting by hand would bypass that.
CTRL="${RUN_ROOT}/data/assembly.control.json"
[ -s "${CTRL}" ] || { echo "FATAL: control 2 has not run (${CTRL} missing)" >&2; exit 1; }
python - "${CTRL}" <<'PY'
import json, sys
a = json.load(open(sys.argv[1]))
if a["from_relation"] == 0:
    print("FATAL: control 2 recorded ZERO areas from relations; refusing planet run")
    sys.exit(1)
print(f"control 2 precondition ok: {a['from_relation']:,} areas from relations on the extract")
PY

watchdog_volume ix1 "${IX1_PATH}" "${IX1_FLOOR_GB}" 300
echo
echo "=== assembling inland water (planet) ==="
START=$(date +%s)
python "${RUN_ROOT}/scripts/assemble_inland_water.py" "${FILTERED}" \
    --out "${ALL}" \
    --stats-out "${RUN_ROOT}/data/assembly.planet.json" \
    --progress-every 500000
echo "elapsed=$(( $(date +%s) - START ))s"
watchdogs_stop
ls -la "${ALL}"

echo
echo "=== splitting into tile layers ==="
python - "${ALL}" "${RUN_ROOT}/data" <<'PY'
import json, os, sys, collections
src, outdir = sys.argv[1], sys.argv[2]
handles, counts = {}, collections.Counter()
with open(src, encoding="utf-8") as fh:
    for line in fh:
        layer = json.loads(line)["properties"]["layer"]
        h = handles.get(layer)
        if h is None:
            h = handles[layer] = open(os.path.join(outdir, f"{layer}.geojsonl"), "w",
                                      encoding="utf-8")
        h.write(line); counts[layer] += 1
for h in handles.values(): h.close()
print("features per layer:")
for k, v in sorted(counts.items()):
    print(f"  {k:10s} {v:12,}")
PY
ls -la "${RUN_ROOT}"/data/*.geojsonl

echo
echo "=== RECONCILIATION: assembly output vs input counts ==="
python - "${RUN_ROOT}/data/counts.filtered.json" "${RUN_ROOT}/data/assembly.planet.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["counts"]
a = json.load(open(sys.argv[2]))
in_ways = c["natural=water"]["way_closed"]
in_rels = c["natural=water"]["relation"]
print(f"  natural=water closed ways IN : {in_ways:,}")
print(f"  natural=water relations   IN : {in_rels:,}")
print(f"  areas from ways          OUT : {a['from_way']:,}")
print(f"  areas from relations     OUT : {a['from_relation']:,}")
print(f"  (OUT counts span all water tags, so they exceed the natural=water")
print(f"   denominators above; the point is that NEITHER source is zero.)")
if a["from_relation"] == 0:
    print("  *** relations produced nothing at planet scale ***"); sys.exit(3)
PY

report_volumes
echo "=== DONE $(date -Is) ==="
