#!/bin/bash
#SBATCH --job-name=water233-verifyfilter
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/05-verifyfilter-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/05-verifyfilter-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G

# place#233 CONTROL 1 — did tags-filter keep what it should?
#
# A tags-filter expression that silently matches nothing produces a small,
# valid, wholly useless PBF, and EVERY subsequent step would succeed on it.
# So compare the filtered file's per-tag way and relation counts against the
# planet's, as a ratio. Counting the filtered file alone cannot do this: it
# would prove only that the file is non-empty.
#
# This costs one extra full pass over the pinned planet. That is the price of
# the control being able to fail, and it is paid once.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
PIN=/ix1/ishi/data/authorities/osm/.pin-place233/planet-pinned.osm.pbf
FILTERED="${RUN_ROOT}/data/water-source.osm.pbf"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
[ -s "${FILTERED}" ] || { echo "FATAL: filtered PBF missing or empty" >&2; exit 1; }

echo
echo "=== counting FILTERED file (fast) ==="
time python "${RUN_ROOT}/scripts/count_water.py" "${FILTERED}" \
    --out "${RUN_ROOT}/data/counts.filtered.json"

echo
echo "=== counting PLANET (the baseline; one full pass) ==="
time python "${RUN_ROOT}/scripts/count_water.py" "${PIN}" \
    --progress-every 5000000 \
    --out "${RUN_ROOT}/data/counts.planet.json"

echo
echo "=== CONTROL 1: filtered vs planet, as ratios ==="
python - "${RUN_ROOT}/data/counts.planet.json" "${RUN_ROOT}/data/counts.filtered.json" <<'PY'
import json, sys
planet = json.load(open(sys.argv[1]))["counts"]
filt   = json.load(open(sys.argv[2]))["counts"]
print(f"{'tag':22s} {'kind':10s} {'planet':>12s} {'filtered':>12s} {'ratio':>8s}  verdict")
worst = 1.0
fail = False
for tag in planet:
    for kind in ("way", "relation"):
        p = planet[tag][kind]; f = filt[tag][kind]
        if p == 0 and f == 0:
            print(f"{tag:22s} {kind:10s} {p:12,} {f:12,} {'--':>8s}  (absent in both)")
            continue
        r = (f / p) if p else float("inf")
        worst = min(worst, r) if p else worst
        if p and r < 0.999:
            verdict = "*** LOSS ***"; fail = True
        elif p and r > 1.001:
            verdict = "unexpected surplus"
        else:
            verdict = "ok"
        print(f"{tag:22s} {kind:10s} {p:12,} {f:12,} {r:8.4f}  {verdict}")
print()
if fail:
    print("CONTROL 1 FAILED: the filtered file is missing objects the planet has.")
    sys.exit(3)
print(f"CONTROL 1 PASSED: every counted tag/kind retained (worst ratio {worst:.4f}).")
PY

report_volumes
echo "=== DONE $(date -Is) ==="
