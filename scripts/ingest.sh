#!/bin/bash
# =============================================================================
# scripts/ingest.sh
# Authority ingestion and data maintenance orchestration
# =============================================================================
# Sourced by es.sh — not intended for standalone execution.
#
# Functions:
#   do_ingest              Submit authority ingestion Slurm job
#   do_ingest_boundaries   Extract OSM/OHM admin boundaries into boundaries index
#   do_generate_tiles      Generate .mbtiles from existing GeoJSON Lines
#   do_augment_ccodes      Spatial country code assignment (nohup on VM)

source "${BASH_SOURCE[0]%/*}/_common.sh"

# =============================================================================
# INGESTION (Slurm batch job)
# =============================================================================

do_ingest() {
    # Check staging is running
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: No staging ES instance running"
        echo "Start one first with: source $0 -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    # Verify ES is responding
    if ! curl -s --connect-timeout 5 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to staging ES at http://${ES_NODE}:${ES_PORT}"
        return 1
    fi

    echo "Staging ES is running at http://${ES_NODE}:${ES_PORT}"

    # Build the Python command with all passed arguments
    PYTHON_ARGS="$@"

    # Create a temporary sbatch script
    INGEST_SCRIPT=$(mktemp /tmp/es-ingest-XXXXXX.sbatch)

    cat > "$INGEST_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-ingest
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=120G
#SBATCH --exclusive
#SBATCH --signal=B:SIGTERM@60
#SBATCH --output=${STAGING_SLURM_LOGS}/ingest-%j.out
#SBATCH --error=${STAGING_SLURM_LOGS}/ingest-%j.err

set -e

echo "=========================================="
echo "AUTHORITY INGESTION JOB"
echo "=========================================="
echo "Started: \$(date)"
echo

# Load environment
source "$ENV_FILE"

# Load staging ES connection info
if [ ! -f "$STAGING_INFO_FILE" ]; then
    echo "ERROR: Staging ES no longer running"
    exit 1
fi
source "$STAGING_INFO_FILE"

# Set ES_HOST for Python scripts
export ES_HOST="http://\${ES_NODE}:\${ES_PORT}"

echo "ES_HOST: \$ES_HOST"
echo "Arguments: $PYTHON_ARGS"
echo

# Activate conda environment
source "$CONDA_SETUP_PATH"
conda activate whg

cd "$REPO_DIR"

# Run ingestion
python -m processing.ingest_all_authorities $PYTHON_ARGS

echo
echo "=========================================="
echo "INGESTION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
SBATCH_EOF

    echo
    echo "Submitting ingestion job..."
    echo "Arguments passed to ingest_all_authorities.py: $PYTHON_ARGS"
    echo

    JOBID=$(sbatch --parsable "$INGEST_SCRIPT")
    rm "$INGEST_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit Slurm job"
        return 1
    fi

    echo "Submitted job: $JOBID"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${STAGING_SLURM_LOGS}/ingest-${JOBID}.out"
    echo
    echo "Note: The staging ES instance must remain running for the duration."
}

# ==============================================================================
# BOUNDARY INDEX INGESTION (Slurm)
# ==============================================================================

do_ingest_boundaries() {
    # Usage: es -ingest-boundaries [OPTIONS]
    #   --source osm|ohm|both   Which PBF source(s) to process (default: both)
    #   --replace                Delete existing boundaries before re-ingesting

    # Check staging is running
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: No staging ES instance running"
        echo "Start one first with: source $0 -staging-start --no-snapshot"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    # Verify ES is responding
    if ! curl -s --connect-timeout 5 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to staging ES at http://${ES_NODE}:${ES_PORT}"
        return 1
    fi

    echo "Staging ES is running at http://${ES_NODE}:${ES_PORT}"

    # Parse arguments
    local SOURCE="both"
    local REPLACE=false
    local NO_TILES=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --source)
                SOURCE="$2"
                shift 2
                ;;
            --replace)
                REPLACE=true
                shift
                ;;
            --no-tiles)
                NO_TILES=true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done


    # Create a temporary sbatch script
    BOUNDARY_SCRIPT=$(mktemp /tmp/es-boundaries-XXXXXX.sbatch)

    cat > "$BOUNDARY_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-boundaries
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --signal=B:SIGTERM@60
#SBATCH --output=${STAGING_SLURM_LOGS}/boundaries-%j.out
#SBATCH --error=${STAGING_SLURM_LOGS}/boundaries-%j.err

set -e

# Raise open file limit — pyosmium's dense_file_array and tippecanoe
# both need many FDs; Slurm default (1024) is too low.
ulimit -n 65536 2>/dev/null || ulimit -n 8192 2>/dev/null || true

echo "=========================================="
echo "BOUNDARY INDEX INGESTION JOB"
echo "=========================================="
echo "Started: \$(date)"
echo "Source: $SOURCE"
echo

# Load environment
source "$ENV_FILE"

# Load staging ES connection info
if [ ! -f "$STAGING_INFO_FILE" ]; then
    echo "ERROR: Staging ES no longer running"
    exit 1
fi
source "$STAGING_INFO_FILE"

# Set ES_HOST for Python scripts
export ES_HOST="http://\${ES_NODE}:\${ES_PORT}"

echo "ES_HOST: \$ES_HOST"
echo

# Activate conda environment
$(activate_environment)

cd "$REPO_DIR"

# Verify osmium-tool is available (needed for fast PBF pre-filtering)
if ! command -v osmium &>/dev/null; then
    echo "WARNING: osmium-tool not found in PATH"
    echo "  The Python script checks fallback locations (~/.local/bin, base conda)"
    echo "  but for reliability, install in the whg env:"
    echo "    conda install -c conda-forge osmium-tool"
    echo
fi

# Check if boundaries index exists, create if not
BOUNDARY_EXISTS=\$(curl -s -o /dev/null -w "%{http_code}" "\$ES_HOST/boundaries")
if [ "\$BOUNDARY_EXISTS" != "200" ]; then
    echo "Creating boundaries index..."
    python -m processing.create_indices 2>/dev/null || {
        # If create_indices fails (places/toponyms already exist), create just boundaries
        python -c "
from elasticsearch import Elasticsearch
import json
es = Elasticsearch('\$ES_HOST', request_timeout=180)
if not es.indices.exists(index='boundaries'):
    with open('schemas/boundaries.json') as f:
        schema = json.load(f)
    es.indices.create(index='boundaries', body=schema, timeout='60s')
    print('boundaries index created')
else:
    print('boundaries index already exists')
"
    }
fi

# Delete existing boundaries if --replace was specified
if [ "$REPLACE" = "true" ]; then
    echo "Deleting existing boundaries..."
    curl -s -X POST "\$ES_HOST/boundaries/_delete_by_query?conflicts=proceed&refresh=true" \\
        -H 'Content-Type: application/json' \\
        -d '{"query":{"match_all":{}}}' | python3 -m json.tool
    echo
fi

# Run boundary extraction
TILE_FLAG=""
if [ "$NO_TILES" = "true" ]; then
    TILE_FLAG="--no-tiles"
fi
python -u -m authorities.osm-boundaries --source $SOURCE \$TILE_FLAG

# Refresh index
curl -s -X POST "\$ES_HOST/boundaries/_refresh" > /dev/null

# Show final count
echo
echo "Boundary index stats:"
curl -s "\$ES_HOST/_cat/indices/boundaries?v"
echo
echo "Counts by namespace:"
curl -s "\$ES_HOST/boundaries/_search" \\
    -H 'Content-Type: application/json' \\
    -d '{"size":0,"aggs":{"by_ns":{"terms":{"field":"namespace"}},"by_level":{"terms":{"field":"admin_level","size":20}}}}' | python3 -m json.tool

echo
echo "=========================================="
echo "BOUNDARY INGESTION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
SBATCH_EOF

    echo
    echo "Submitting boundary ingestion job..."
    echo "  Source: $SOURCE"
    echo "  Replace existing: $REPLACE"
    echo "  Generate tiles: $([ "$NO_TILES" = "true" ] && echo "no" || echo "yes")"
    echo

    JOBID=$(sbatch --parsable "$BOUNDARY_SCRIPT")
    rm "$BOUNDARY_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit Slurm job"
        return 1
    fi

    echo "Submitted job: $JOBID"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${STAGING_SLURM_LOGS}/boundaries-${JOBID}.out"
    echo
    echo "Note: The staging ES instance must remain running for the duration."
}


# ==============================================================================
# GENERATE BOUNDARY TILES (Slurm)
# ==============================================================================

do_generate_tiles() {
    # Usage: es -generate-tiles
    # Submits a short Slurm job to run tippecanoe on the existing GeoJSON Lines file.

    local GEOJSONL="${DATA_DIR}/boundaries/boundaries.geojsonl"
    if [ ! -f "$GEOJSONL" ]; then
        echo "ERROR: GeoJSON Lines file not found: $GEOJSONL"
        echo "  Run 'es -ingest-boundaries' first to generate it."
        return 1
    fi

    local SIZE=$(du -h "$GEOJSONL" | cut -f1)
    echo "GeoJSON Lines file: $GEOJSONL ($SIZE)"

    TILES_SCRIPT=$(mktemp /tmp/es-tiles-XXXXXX.sbatch)

    cat > "$TILES_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-tiles
#SBATCH --partition=smp
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=${STAGING_SLURM_LOGS}/tiles-%j.out
#SBATCH --error=${STAGING_SLURM_LOGS}/tiles-%j.err

set -e

echo "=========================================="
echo "BOUNDARY TILE GENERATION JOB"
echo "=========================================="
echo "Started: \$(date)"
echo

# Load environment
source "$ENV_FILE"

# Activate conda environment
$(activate_environment)

cd "$REPO_DIR"

python -u -c "
import importlib
mod = importlib.import_module('authorities.osm-boundaries')
mod.generate_mbtiles(mod.GEOJSONL_FILE, mod.MBTILES_FILE)
"

echo
echo "=========================================="
echo "TILE GENERATION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
SBATCH_EOF

    echo "Submitting tile generation job..."
    JOBID=$(sbatch --parsable "$TILES_SCRIPT")
    rm "$TILES_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit Slurm job"
        return 1
    fi

    echo "Submitted job: $JOBID"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${STAGING_SLURM_LOGS}/tiles-${JOBID}.out"
}
# =============================================================================
# AUGMENT CCODES (spatial country code assignment)
# =============================================================================

do_augment_ccodes() {
    # Usage: es -augment-ccodes [OPTIONS]
    #
    # Augment places with ccodes by spatially intersecting each place's
    # geometry against full-resolution Natural Earth country polygons.
    # Runs directly on the production ES instance.
    #
    # The process runs under nohup so it survives SSH disconnection.
    # Output is logged to a timestamped file; tail -f is started for
    # immediate feedback (Ctrl-C the tail without killing the job).
    #
    # Options are passed through to processing.augment_ccodes (see --help).
    # ES host and password are injected automatically.

    local ES_URL="${PROD_ES_URL:-http://localhost:${PROD_ES_INTERNAL_PORT:-9201}}"
    local PASS_FILE="${IX1_BASE}/es/config/elastic.password"
    local LOG_DIR="${IX1_BASE}/es/logs"
    local TIMESTAMP
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    local LOG_FILE="${LOG_DIR}/augment_ccodes_${TIMESTAMP}.log"

    mkdir -p "$LOG_DIR"

    # Verify ES is responding
    if ! es_curl --connect-timeout 5 "${ES_URL}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to production ES at ${ES_URL}"
        return 1
    fi

    echo "=========================================="
    echo "AUGMENT CCODES"
    echo "=========================================="
    echo "ES:   ${ES_URL}"
    echo "Args: $*"
    echo "Log:  ${LOG_FILE}"
    echo

    # Build auth args
    local AUTH_ARGS=""
    if [ -f "$PASS_FILE" ]; then
        AUTH_ARGS="--es-pass-file ${PASS_FILE}"
    fi

    # Write a small wrapper script so nohup runs in the right env
    local WRAPPER
    WRAPPER=$(mktemp /tmp/augment-ccodes-XXXXXX.sh)

    cat > "$WRAPPER" <<WRAPPER_EOF
#!/bin/bash
# Activate conda — try known paths, fall back to conda already in PATH
for _cs in "$CONDA_SETUP_PATH" \
           "/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh" \
           "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh" \
           "\$HOME/miniconda3/etc/profile.d/conda.sh" \
           "\$HOME/anaconda3/etc/profile.d/conda.sh"; do
    [ -f "\$_cs" ] && source "\$_cs" && break
done

conda activate whg 2>/dev/null || true
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"
cd "$REPO_DIR"

python -u -m processing.augment_ccodes \\
    --es-host "${ES_URL}" \\
    ${AUTH_ARGS} \\
    $@
WRAPPER_EOF

    chmod +x "$WRAPPER"

    # Launch under nohup; redirect stdout+stderr to log file
    nohup bash "$WRAPPER" > "$LOG_FILE" 2>&1 &
    local PID=$!

    # Clean up the temp script after a short delay (process has already read it)
    (sleep 5 && rm -f "$WRAPPER") &

    echo "Started PID ${PID}"
    echo "Safe to disconnect — the process will continue running."
    echo
    echo "Monitor with:"
    echo "  tail -f ${LOG_FILE}"
    echo
    echo "Check status:"
    echo "  ps -p ${PID} -o pid,etime,args"
    echo
    echo "--- Attaching to log (Ctrl-C to detach without stopping the job) ---"
    echo

    tail -f "$LOG_FILE" --pid="$PID"
}
