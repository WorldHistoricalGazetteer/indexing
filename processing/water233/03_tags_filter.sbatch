#!/bin/bash
#SBATCH --job-name=water233-tagsfilter
#SBATCH --output=/ix1/ishi/water233-20260902T220703Z/logs/03-tagsfilter-%j.out
#SBATCH --error=/ix1/ishi/water233-20260902T220703Z/logs/03-tagsfilter-%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --account=ishi
#SBATCH --nodes=1 --ntasks=1 --cpus-per-task=8 --mem=32G

# place#233 Step 0c — tags-filter the pinned planet down to water.
#
# This is the step that makes everything after it tractable. Area assembly
# needs the location of every node referenced by every candidate way; against
# the raw 87.5 GB planet that means >100 GB of RAM or a slow disk-backed
# index. Against this filtered extract the node index fits in memory.
#
# tags-filter INCLUDES referenced objects by default (-R is --omit-referenced,
# the flag that turns it OFF), so the output carries the member ways and nodes
# needed to build areas, and it retains natural=coastline so ONE filtered file
# serves both the ocean step and the inland-water step.
#
# Writes to /ix1 (1837 GB free, no Elasticsearch), NOT /vast (226 GB free,
# shared with production ES).

set -eo pipefail
source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg

# Do NOT export LD_LIBRARY_PATH. osmium needs conda's libs; ogr2ogr BREAKS
# under them (system libldap vs conda OpenSSL 3 -> undefined symbol EVP_md2).
# An export poisons every later process in the shell; a per-command prefix
# scopes it to the one binary that needs it.
OSMIUM="env LD_LIBRARY_PATH=$CONDA_PREFIX/lib osmium"

RUN_ROOT=/ix1/ishi/water233-20260902T220703Z
source "${RUN_ROOT}/scripts/_guards.sh"

PIN=/ix1/ishi/data/authorities/osm/.pin-place233/planet-pinned.osm.pbf
OUT="${RUN_ROOT}/data/water-source.osm.pbf"
TMP="${OUT}.inprogress"
EXPECTED_WRITE_GB="${EXPECTED_WRITE_GB:-40}"

echo "=== node $(hostname) job ${SLURM_JOB_ID} start $(date -Is) ==="
echo "cpus=${SLURM_CPUS_PER_TASK} mem=${SLURM_MEM_PER_NODE}M scratch=${SLURM_SCRATCH:-none}"

echo
echo "=== verifying pin (refuse if the source moved under us) ==="
python - "${RUN_ROOT}/pin.json" "${PIN}" <<'PY'
import json, os, sys
rec = json.load(open(sys.argv[1])); st = os.stat(sys.argv[2])
bad = []
if st.st_ino  != rec["inode"]:      bad.append(f"inode {st.st_ino} != pinned {rec['inode']}")
if st.st_size != rec["size_bytes"]: bad.append(f"size {st.st_size} != pinned {rec['size_bytes']}")
if bad:
    print("FATAL: " + "; ".join(bad)); sys.exit(1)
print(f"pin verified: inode {st.st_ino} size {st.st_size}")
PY

guard_selftest "${VAST_PATH}"
guard_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 0
guard_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}"  "${EXPECTED_WRITE_GB}"
report_volumes
watchdog_volume vast "${VAST_PATH}" "${VAST_FLOOR_GB}" 120
watchdog_volume ix1  "${IX1_PATH}"  "${IX1_FLOOR_GB}"  120

rm -f "${TMP}"
echo
echo "=== [0c] osmium tags-filter ==="
echo "in : ${PIN}"
echo "out: ${OUT}"
START=$(date +%s)

# Written to .inprogress and renamed only on success. A truncated PBF is the
# dangerous artefact here: it is valid, it is small, and every downstream step
# would succeed on it.
set +e
${OSMIUM} tags-filter \
    "${PIN}" \
    w/natural=coastline \
    nwr/natural=water \
    nwr/water \
    nwr/waterway \
    nwr/landuse=reservoir \
    --output "${TMP}" \
    --output-format pbf \
    --overwrite
RC=$?
set -e
END=$(date +%s)
echo "tags-filter rc=${RC} elapsed=$(( END - START ))s ($(( (END-START)/60 )) min)"
watchdogs_stop
[ "${RC}" -eq 0 ] || { echo "FATAL: tags-filter failed rc=${RC}" >&2; exit "${RC}"; }

mv "${TMP}" "${OUT}"
echo "renamed into place:"; ls -la "${OUT}"

echo
echo "=== output fileinfo (extended) ==="
${OSMIUM} fileinfo -e "${OUT}" 2>&1 | head -45

echo
echo "=== check-refs: are referenced objects actually present? ==="
${OSMIUM} check-refs -r "${OUT}" 2>&1 | tail -20 || echo "check-refs reported problems (see above)"

report_volumes
echo "=== DONE $(date -Is) ==="
