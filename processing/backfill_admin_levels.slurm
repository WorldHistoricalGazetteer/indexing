#!/bin/bash
#SBATCH --job-name=geoscheme_bounds
#SBATCH --output=/ix1/ishi/logs/geoscheme_bounds_%j.log
#SBATCH --error=/ix1/ishi/logs/geoscheme_bounds_%j.err
#SBATCH --time=01:00:00
#SBATCH --mem=32G
#SBATCH --cpus-per-task=2

# =============================================================================
# Generate UN M49 geoscheme boundaries (continents + subregions) + Antarctica.
#
# Derives admin_level 0 (continents) and 1 (subregions) by unioning the
# admin_level=2 country boundaries in the STAGED `un` tree, and fetches
# Antarctica from the Overpass API.
#
# PREREQUISITE — stage `un` first, or this job exits non-zero:
#   WHG_STAGING_MODE=1 python -m authorities.un-countries
#
# Usage:
#   sbatch -M htc --account=ishi processing/backfill_admin_levels.slurm
#
# ⚠ This job reads STAGED FILES, not Elasticsearch. It takes no arguments and
#   has no staging/production distinction — see the note at the invocation.
# =============================================================================

set -euo pipefail

# ---- This script takes no arguments; fail loudly rather than ignore them ----
# It used to accept --production to pick an ES target. The module no longer
# reads ES at all, so silently accepting that flag would run something other
# than what the caller asked for.
if [ "$#" -gt 0 ]; then
    echo "ERROR: this job takes no arguments (got: $*)"
    echo "  --production no longer applies: un-geoscheme-boundaries reads the"
    echo "  staged 'un' tree, not Elasticsearch. There is no ES target to pick."
    exit 2
fi

# ---- Conda environment ----
CONDA_SETUP="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP" ]; then
    CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
# conda's activation scripts are not -u clean
set +u
source "$CONDA_SETUP"
conda activate whg
set -u

# Repo root moved /ix1 -> /vast on 2026-05-01 (imports and log writes on flash).
REPO_ROOT="/vast/ishi/elastic"
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
cd "$REPO_ROOT"

echo "================================================"
echo "UN M49 GEOSCHEME BOUNDARIES + ANTARCTICA"
echo "================================================"
echo "Job ID:    ${SLURM_JOB_ID:-<none>}"
echo "Node:      $(hostname)"
echo "Time:      $(date)"
echo "Python:    $(which python)"
echo "Repo:      $REPO_ROOT"
echo ""

mkdir -p /ix1/ishi/logs

# ---- Run geoscheme boundary generation ----
# Unions the staged admin_level=2 country geometries into 5 continents +
# 22 subregions + 2 intermediary regions, and fetches Antarctica from Overpass.
#
# ⚠ WHG_STAGING_MODE=1 is REQUIRED — the module writes through the staged
#   place-doc writer, and without it the staged `un` tree is not read.
# ⚠ Antarctica needs outbound network from the compute node. The module falls
#   back to a second Overpass endpoint and then skips Antarctica with a
#   warning, so a blocked node degrades the result rather than failing the job
#   — check the log for "Antarctica skipped" rather than trusting the exit code.
WHG_STAGING_MODE=1 python -u -m authorities.un-geoscheme-boundaries

echo ""
echo "Completed at $(date)"
