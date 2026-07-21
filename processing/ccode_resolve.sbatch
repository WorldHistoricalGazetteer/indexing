#!/bin/bash
#SBATCH --job-name=ccode-resolve
#SBATCH --account=ishi
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --output=/ix1/ishi/logs/ccode_resolve_%A_%a.log
#SBATCH --error=/ix1/ishi/logs/ccode_resolve_%A_%a.err
#SBATCH --time=03:00:00
#SBATCH --mem=8G
#SBATCH --cpus-per-task=2
#SBATCH --array=0-15%16

# =============================================================================
# Full-corpus ccode re-derivation — RESOLVE stage (GitHub issue place#126).
#
# MUST run on CRC Slurm compute, NEVER on the pitt VM: this stage is CPU + /vast
# IO heavy and running it 8-wide on pitt on 2026-07-21 OOM-thrashed the VM and
# took prod ES down for ~1h. See developer/plan-ccode-corpus-rerun.md §5.
#
# Reads (no ES access):
#   - /vast/ishi/ccodefix2/targets.*.jsonl   (exported on pitt, carries current ccodes)
#   - processing/data/un_bnda_countries.geojson  (BNDA country boundaries)
#   - /vast/ishi/geom                         (geom store — full polygons for AREA docs)
# Writes:
#   - /vast/ishi/ccodefix2/patch.<task>.jsonl  (changed-only ccode patches; [] = clear)
#
# Each array task resolves the modulo slice  line_no % OF == SLURM_ARRAY_TASK_ID
# of the combined targets glob.  OF MUST equal the array width (default 16).
#
# Submit (from a CRC login node, e.g. crc0):
#   sbatch -M htc processing/ccode_resolve.sbatch
# Small validation (one task over all lines of the glob):
#   sbatch -M htc --array=0-0 --export=ALL,OF=1 processing/ccode_resolve.sbatch
# =============================================================================

OF="${OF:-16}"
DIR="${CCODE_DIR:-/vast/ishi/ccodefix2}"
SNAP="${SNAP_DEG:-0.075}"
CONDA_SH="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
[ -f "$CONDA_SH" ] || CONDA_SH="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"

source "$CONDA_SH"
conda activate whg
cd /vast/ishi/elastic
export PYTHONPATH=/vast/ishi/elastic
mkdir -p /ix1/ishi/logs

echo "================================================================"
echo "ccode RESOLVE  task=${SLURM_ARRAY_TASK_ID}/OF=${OF}  snap=${SNAP}"
echo "node=$(hostname)  time=$(date)  python=$(which python)"
echo "================================================================"

python -u -m processing.backfill_ccodes resolve --overwrite \
    --in "${DIR}/targets.*.jsonl" \
    --out "${DIR}/patch.${SLURM_ARRAY_TASK_ID}.jsonl" \
    --slice "${SLURM_ARRAY_TASK_ID}" --of "${OF}" \
    --snap-deg "${SNAP}"

echo "=== task ${SLURM_ARRAY_TASK_ID} done $(date) ==="
