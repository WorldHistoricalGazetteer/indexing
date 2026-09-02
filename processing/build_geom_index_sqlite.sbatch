#!/bin/bash
#SBATCH --job-name=geom_index_sqlite
#SBATCH --output=/ix1/ishi/logs/%x_%j.log
#SBATCH --error=/ix1/ishi/logs/%x_%j.err
#SBATCH --time=02:00:00
#SBATCH --qos=htc-htc-s
#SBATCH --mem=8G
#SBATCH --cpus-per-task=2
#
# One-off backfill of the geom store's index.sqlite (place#165), plus the
# byte-identity verification that must pass before the gateway is restarted.
#
#   sbatch -M htc --account=ishi --export=ALL processing/build_geom_index_sqlite.sbatch
#
# Runs build then verify; a failed verify fails the job. 8G covers the
# builder's ~1.5 GB page cache with room to spare — deliberately NOT the
# 5.4 GB the JSON index would have needed.

set -o pipefail

STORE_DIR="${STORE_DIR:-/vast/ishi/geom}"
SAMPLE="${SAMPLE:-10000}"

echo "=== geom index sqlite: ${STORE_DIR} ==="
echo "Host: $(hostname)  Job: ${SLURM_JOB_ID:-none}  Started: $(date -Is)"

CONDA_SETUP="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
[ -f "$CONDA_SETUP" ] || CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
source "$CONDA_SETUP"
conda activate whg

cd "${SLURM_SUBMIT_DIR:-/vast/ishi/elastic}" || exit 1

df -h /vast/ishi | tail -1   # MUST be /vast/ishi (our 1T quota); bare /vast reports
                             # the whole 3.9P VAST pool and is useless as a headroom signal

echo "--- BUILD ---"
python -u -m processing.build_geom_index_sqlite build --store-dir "$STORE_DIR" || exit 1

echo "--- VERIFY (${SAMPLE} keys, byte-for-byte through both paths) ---"
python -u -m processing.build_geom_index_sqlite verify \
    --store-dir "$STORE_DIR" --sample "$SAMPLE" || exit 1

echo "--- STAT ---"
python -u -m processing.build_geom_index_sqlite stat --store-dir "$STORE_DIR"

ls -la --time-style=long-iso "${STORE_DIR}"/index.*
df -h /vast/ishi | tail -1   # MUST be /vast/ishi (our 1T quota); bare /vast reports
                             # the whole 3.9P VAST pool and is useless as a headroom signal
echo "Finished: $(date -Is)"
