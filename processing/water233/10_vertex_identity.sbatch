#!/bin/bash
#SBATCH --job-name=water233-vertexid
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/10-vertexid-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/10-vertexid-%j.err
#SBATCH --time=03:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=32G

# place#233 — evidence for the SAME-EDITION premise, measured not asserted.
#
# The issue argues that extracting from our own PBF buys vertex-identity
# rather than a closer approximation, because coastal admin boundaries share
# the coastline WAY. That is a checkable claim about the data, and checking it
# does not require deploying anything -- which matters, because the post-build
# visual criterion cannot be evaluated until the tileset is deployed and the
# deploy is out of scope here.
#
# Cut from the PINNED planet (all tags -- the water-filtered extract has no
# admin boundaries in it, by construction).
#
# Neum bbox is the same viewport used for the pre-build known-bad control, so
# the two pieces of evidence describe the same place.

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
OSMIUM="env LD_LIBRARY_PATH=$CONDA_PREFIX/lib osmium"

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"
PIN=/ix1/ishi/data/authorities/osm/.pin-place233/planet-pinned.osm.pbf
EXTRACT="${RUN_ROOT}/data/neum-alltags.osm.pbf"
BBOX="${BBOX:-16.9,42.3,18.4,43.2}"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}" 5

echo
echo "=== cutting all-tags extract from the PINNED planet, bbox=${BBOX} ==="
echo "    (one pass over 87.5 GB; the extract is small)"
START=$(date +%s)
${OSMIUM} extract --bbox "${BBOX}" -s smart \
    --output "${EXTRACT}" --overwrite "${PIN}"
echo "extract elapsed=$(( $(date +%s) - START ))s"
ls -la "${EXTRACT}"
${OSMIUM} fileinfo -e "${EXTRACT}" 2>&1 | grep -E "Number of|Bounding|replication" | head -12

echo
echo "=== do coastal admin boundaries SHARE coastline ways/nodes? ==="
python "${RUN_ROOT}/scripts/vertex_identity.py" "${EXTRACT}" \
    --out "${RUN_ROOT}/data/vertex_identity.neum.json" \
    --min-admin-level 2 --max-admin-level 8

report_volumes
echo "=== DONE $(date -Is) ==="
