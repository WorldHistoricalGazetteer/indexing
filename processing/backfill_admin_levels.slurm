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
# existing admin_level=2 country boundaries already in the boundaries index.
# Also fetches Antarctica from the Overpass API.
#
# Usage:
#   sbatch processing/backfill_admin_levels.slurm              # staging ES
#   sbatch processing/backfill_admin_levels.slurm --production # production ES
# =============================================================================

set -e

# ---- Parse arguments ----
USE_PRODUCTION=false
for arg in "$@"; do
    case "$arg" in
        --production) USE_PRODUCTION=true ;;
    esac
done

# ---- Conda environment ----
CONDA_SETUP="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP" ]; then
    CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
source "$CONDA_SETUP"
conda activate whg

export PYTHONPATH="/ix1/ishi/elastic:${PYTHONPATH}"

echo "================================================"
echo "UN M49 GEOSCHEME BOUNDARIES + ANTARCTICA"
echo "================================================"
echo "Job ID:    $SLURM_JOB_ID"
echo "Node:      $(hostname)"
echo "Time:      $(date)"
echo "Python:    $(which python)"
echo ""

# ---- Determine ES target ----
if $USE_PRODUCTION; then
    ES_PASS=$(cat /ix1/ishi/es/config/elastic.password 2>/dev/null || true)
    if [ -n "$ES_PASS" ]; then
        ES_TARGET="http://elastic:${ES_PASS}@localhost:9201"
    else
        ES_TARGET="http://localhost:9201"
    fi
    echo "Target: PRODUCTION ES (localhost:9201)"
else
    STAGING_INFO="/ix1/ishi/esinfo/es-staging.env"
    if [ -f "$STAGING_INFO" ]; then
        source "$STAGING_INFO"
        ES_TARGET="http://${ES_NODE}:${ES_PORT}"
        echo "Target: STAGING ES (${ES_NODE}:${ES_PORT})"
    else
        echo "ERROR: No staging ES found. Start one with:"
        echo "  source scripts/es.sh -staging-start --no-snapshot"
        echo "Or use --production to target production ES."
        exit 1
    fi
fi
echo ""

# ---- Ensure log directory exists ----
mkdir -p /ix1/ishi/logs

# ---- Run geoscheme boundary generation ----
# Fetches level-2 country geometries from ES, unions them into
# 5 continents + 22 subregions + 2 intermediary regions,
# and fetches Antarctica from Overpass.
python -u -m authorities.un-geoscheme-boundaries \
    --es-host "$ES_TARGET"

echo ""
echo "Completed at $(date)"
