#!/bin/bash
#SBATCH --job-name=water233-recount
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/11-recount-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/11-recount-%j.err
#SBATCH --time=01:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 — resolve the control-2 ways row, which did not reconcile:
#   natural=water closed ways IN : 6,123
#   areas from closed ways   OUT : 6,721   (+9.8%, a ratio above 1)
#
# A closed way either assembles into one area or fails, so OUT must be <= IN
# when both count the same population. The suspicion is that they did not:
# IN was natural=water only, OUT was every tag the assembler accepts. Rather
# than relabel the column, count_water.py now IMPORTS the assembler's own
# classify() and reports a union row, so the two tables count the same
# population by construction and cannot drift apart again.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
EXTRACT="${RUN_ROOT}/data/control-saimaa.osm.pbf"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
[ -s "${EXTRACT}" ] || { echo "FATAL: control extract missing" >&2; exit 1; }

python "${RUN_ROOT}/scripts/count_water.py" "${EXTRACT}" \
    --out "${RUN_ROOT}/data/counts.control.union.json"

echo
echo "=== RECONCILIATION with the shared predicate ==="
python - "${RUN_ROOT}/data/counts.control.union.json" \
        "${RUN_ROOT}/data/assembly.control.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["counts"]
a = json.load(open(sys.argv[2]))
u = c["ANY (assembler classify union)"]
print(f"  union closed ways      IN : {u['way_closed']:,}")
print(f"  areas from ways       OUT : {a['from_way']:,}")
print(f"  union relations        IN : {u['relation']:,}")
print(f"  areas from relations  OUT : {a['from_relation']:,}")
print()
wr = a["from_way"] / u["way_closed"] if u["way_closed"] else float("inf")
rr = a["from_relation"] / u["relation"] if u["relation"] else float("inf")
print(f"  ways ratio      OUT/IN = {wr:.4f}")
print(f"  relations ratio OUT/IN = {rr:.4f}")
print()
ok = True
if wr > 1.0001:
    print("  *** ways ratio STILL above 1: the populations still differ,")
    print("  *** so the shared-predicate hypothesis is WRONG and this needs")
    print("  *** a real investigation, not a relabel.")
    ok = False
else:
    print(f"  ways ratio <= 1 as it must be: {u['way_closed'] - a['from_way']:,} "
          f"closed ways did not become areas (self-intersecting, degenerate, "
          f"or not a valid ring).")
if rr > 1.0001:
    print(f"  NOTE relations ratio {rr:.4f} > 1: a relation with disjoint outer")
    print( "  rings legitimately yields more than one area, so a small excess")
    print( "  here is expected rather than an error.")
sys.exit(0 if ok else 3)
PY
echo "=== DONE $(date -Is) ==="
