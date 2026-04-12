#!/bin/bash
# =============================================================================
# scripts/ingest.sh
# Authority ingestion and data maintenance orchestration
# =============================================================================
# Sourced by es.sh — not intended for standalone execution.
#
# Functions:
#   do_ingest              Submit authority ingestion Slurm job
#   do_boundary_pass       Assemble full boundary geometry from PBF (Slurm)
#   do_generate_tiles      Generate .mbtiles from boundary places in ES (Slurm)
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
# BOUNDARY PASS (Slurm) — assemble full geometry for boundary relations
# ==============================================================================

do_boundary_pass() {
    # Usage: es -boundary-pass [OPTIONS]
    #   --source osm|ohm|both   Which PBF source(s) to process (default: both)

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

    # Parse arguments
    local SOURCE="both"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --source)
                SOURCE="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    BOUNDARY_SCRIPT=$(mktemp /tmp/es-boundary-pass-XXXXXX.sbatch)

    cat > "$BOUNDARY_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-boundary-pass
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --signal=B:SIGTERM@60
#SBATCH --output=${STAGING_SLURM_LOGS}/boundary-pass-%j.out
#SBATCH --error=${STAGING_SLURM_LOGS}/boundary-pass-%j.err

set -e

ulimit -n 65536 2>/dev/null || ulimit -n 8192 2>/dev/null || true

echo "=========================================="
echo "BOUNDARY PASS JOB"
echo "=========================================="
echo "Started: \$(date)"
echo "Source: $SOURCE"
echo

source "$ENV_FILE"

if [ ! -f "$STAGING_INFO_FILE" ]; then
    echo "ERROR: Staging ES no longer running"
    exit 1
fi
source "$STAGING_INFO_FILE"
export ES_HOST="http://\${ES_NODE}:\${ES_PORT}"

echo "ES_HOST: \$ES_HOST"
echo

$(activate_environment)

cd "$REPO_DIR"

# Run boundary pass for each source
if [ "$SOURCE" = "both" ] || [ "$SOURCE" = "osm" ]; then
    python -u -m authorities.osm-boundary-pass --source osm
fi
if [ "$SOURCE" = "both" ] || [ "$SOURCE" = "ohm" ]; then
    python -u -m authorities.osm-boundary-pass --source ohm
fi

# Refresh index
curl -s -X POST "\$ES_HOST/places/_refresh" > /dev/null

echo
echo "=========================================="
echo "BOUNDARY PASS COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
SBATCH_EOF

    echo
    echo "Submitting boundary pass job..."
    echo "  Source: $SOURCE"
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
    echo "  tail -f ${STAGING_SLURM_LOGS}/boundary-pass-${JOBID}.out"
}


# ==============================================================================
# GENERATE BOUNDARY TILES (Slurm)
# ==============================================================================

do_generate_tiles() {
    # Usage: es -generate-tiles [--es-host URL] [--authority NAMESPACE]
    # Submits a Slurm job to generate .mbtiles from boundary places in ES.

    local ES_URL=""
    local AUTHORITY=""
    local DEPLOY=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --es-host)
                ES_URL="$2"
                shift 2
                ;;
            --authority)
                AUTHORITY="--authority $2"
                shift 2
                ;;
            --deploy)
                DEPLOY="--deploy"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    # Default ES URL
    if [ -z "$ES_URL" ]; then
        if [ -f "$STAGING_INFO_FILE" ]; then
            source "$STAGING_INFO_FILE"
            ES_URL="http://${ES_NODE}:${ES_PORT}"
        else
            ES_URL="${PROD_ES_URL:-http://localhost:${PROD_ES_INTERNAL_PORT:-9201}}"
        fi
    fi

    echo "ES host: $ES_URL"

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
echo "TILESET GENERATION JOB"
echo "=========================================="
echo "Started: \$(date)"
echo

source "$ENV_FILE"

$(activate_environment)

cd "$REPO_DIR"

python -u -m processing.generate_tiles --es-host "$ES_URL" $AUTHORITY $DEPLOY

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
