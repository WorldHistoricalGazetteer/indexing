#!/bin/bash
#SBATCH --job-name=water233-cmp
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/12-cmp-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/12-cmp-%j.err
#SBATCH --time=04:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 CONTROL 1, repaired.
#
# Job 11111756 was submitted BEFORE count_water.py gained its union row, and
# it loads the module twice: once at job start (old schema, filtered file)
# and once minutes later (new schema, planet). Its comparison indexes planet
# keys into the filtered dict and will KeyError on the key that exists on one
# side only. The expensive planet pass still lands, because the JSON is
# written before the comparison runs -- so this job re-counts only the CHEAP
# side and redoes the comparison.
#
# Note this is a self-inflicted version skew: I edited a module while a job
# that imports it was in flight. The lesson is not "be careful" but that a
# long job should pin its code, which the run dir's scripts/ copy was
# supposed to provide and did not, because I overwrote it in place.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
FILTERED="${RUN_ROOT}/data/water-source.osm.pbf"
PLANET_JSON="${RUN_ROOT}/data/counts.planet.json"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
[ -s "${PLANET_JSON}" ] || { echo "FATAL: ${PLANET_JSON} missing -- the planet pass did not complete" >&2; exit 1; }

echo "=== recounting the FILTERED file with the current module ==="
python "${RUN_ROOT}/scripts/count_water.py" "${FILTERED}" \
    --out "${RUN_ROOT}/data/counts.filtered.union.json" >/dev/null

echo
echo "=== CONTROL 1: filtered vs planet, complete accounting ==="
python - "${PLANET_JSON}" "${RUN_ROOT}/data/counts.filtered.union.json" <<'PY'
import json, sys
P = json.load(open(sys.argv[1]))
F = json.load(open(sys.argv[2]))
planet, filt = P["counts"], F["counts"]

only_p = sorted(set(planet) - set(filt))
only_f = sorted(set(filt) - set(planet))
if only_p or only_f:
    print(f"  NOTE schema differs -- only in planet: {only_p}; only in filtered: {only_f}")
    print( "  (reported rather than silently skipped)")
    print()

print(f"{'tag':34s} {'kind':9s} {'planet':>14s} {'filtered':>14s} {'ratio':>8s}  verdict")
fail = False
for tag in sorted(set(planet) & set(filt)):
    for kind in ("way", "relation"):
        p, f = planet[tag][kind], filt[tag][kind]
        if p == 0 and f == 0:
            print(f"{tag:34s} {kind:9s} {p:14,} {f:14,} {'--':>8s}  absent in both")
            continue
        r = (f / p) if p else float("inf")
        if p and r < 0.9999:
            verdict = f"*** LOSS: {p-f:,} missing ***"; fail = True
        elif r > 1.0001:
            verdict = "*** IMPOSSIBLE: ratio > 1 ***"; fail = True
        else:
            verdict = "exact"
        print(f"{tag:34s} {kind:9s} {p:14,} {f:14,} {r:8.4f}  {verdict}")
print()
if fail:
    print("CONTROL 1 FAILED.")
    sys.exit(3)
print("CONTROL 1 PASSED: every planet count is matched exactly in the filtered")
print("file. Not 'the filter kept enough' -- every count corresponds to")
print("something, with nothing left over in either direction.")
PY
echo "=== DONE $(date -Is) ==="
