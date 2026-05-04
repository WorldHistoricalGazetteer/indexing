#!/bin/bash
# =============================================================================
# scripts/ingest.sh
# Authority ingestion and data maintenance orchestration
# =============================================================================
# Sourced by es.sh — not intended for standalone execution.
#
# Functions:
#   do_ingest              Submit authority ingestion Slurm job
#   do_boundary_pass       Re-run OSM/OHM boundary geometry completion manually (Slurm)
#   do_generate_tiles      Generate .mbtiles from staged boundary places (Slurm array, no ES)
#   (do_augment_ccodes retired — see processing/ccode_enrichment.py + submit_ccode_slurm.py)

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

    # Parse wrapper-only options; forward the rest to ingest_all_authorities.py
    local SUBMIT_H3_ARRAY=0
    local DEFER_H3=0
    local RUN_ID=""
    local FORWARD_ARGS=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --submit-h3-array)
                SUBMIT_H3_ARRAY=1
                shift
                ;;
            --defer-h3)
                DEFER_H3=1
                FORWARD_ARGS+=("$1")
                shift
                ;;
            --run-id)
                RUN_ID="$2"
                FORWARD_ARGS+=("$1" "$2")
                shift 2
                ;;
            *)
                FORWARD_ARGS+=("$1")
                shift
                ;;
        esac
    done

    if [ "$SUBMIT_H3_ARRAY" = "1" ] && [ "$DEFER_H3" != "1" ]; then
        DEFER_H3=1
        FORWARD_ARGS+=("--defer-h3")
    fi

    if [ "$SUBMIT_H3_ARRAY" = "1" ] && [ -z "$RUN_ID" ]; then
        RUN_ID="ingest-$(date -u +%Y%m%dT%H%M%SZ)"
        FORWARD_ARGS+=("--run-id" "$RUN_ID")
    fi

    # Build the Python command with all forwarded arguments
    PYTHON_ARGS="${FORWARD_ARGS[*]}"

    # Create a temporary sbatch script
    INGEST_SCRIPT=$(mktemp /tmp/es-ingest-XXXXXX.sbatch)

    cat > "$INGEST_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-ingest
#SBATCH --partition=smp
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=120G
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
    echo "  tail -f ${STAGING_SLURM_LOGS}/ingest-${JOBID}.*"
    echo
    echo "Note: The staging ES instance must remain running for the duration."

    if [ "$SUBMIT_H3_ARRAY" = "1" ]; then
        if [ -z "$RUN_ID" ]; then
            echo "ERROR: expected run id for H3 array submission"
            return 1
        fi
        echo
        echo "Submitting dependent boundary -> boundary_merge -> H3 chain for run id: $RUN_ID"

        do_boundary_stage_array --run-id "$RUN_ID" --dependency "$JOBID" || return 1
        local BOUNDARY_STAGE_JOBID="$LAST_SUBMITTED_JOBID"

        do_boundary_merge_array --run-id "$RUN_ID" --dependency "$BOUNDARY_STAGE_JOBID" || return 1
        local BOUNDARY_MERGE_JOBID="$LAST_SUBMITTED_JOBID"

        do_h3_stage_array --run-id "$RUN_ID" --dependency "$BOUNDARY_MERGE_JOBID" || return 1
    fi
}


# =============================================================================
# BOUNDARY STAGE (Slurm array, staged-only, osm/ohm only)
# =============================================================================

do_boundary_stage_array() {
    local RUN_ID=""
    local DEPENDENCY_JOBID=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run-id)
                RUN_ID="$2"
                shift 2
                ;;
            --dependency)
                DEPENDENCY_JOBID="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    if [ -z "$RUN_ID" ]; then
        echo "ERROR: --run-id is required for boundary stage array"
        return 1
    fi

    local RUN_MANIFEST="${STAGED_RUNS_DIR}/${RUN_ID}.json"
    if [ ! -f "$RUN_MANIFEST" ]; then
        echo "ERROR: Run manifest not found: $RUN_MANIFEST"
        return 1
    fi

    local TASK_COUNT
    TASK_COUNT=$(python3 - <<PY
import json
from pathlib import Path
manifest = json.loads(Path("$RUN_MANIFEST").read_text(encoding="utf-8"))
selected = manifest.get("selected_namespaces", [])
targets = [ns for ns in selected if ns in ("osm", "ohm")]
print(len(targets))
PY
)

    if [ -z "$TASK_COUNT" ] || [ "$TASK_COUNT" -le 0 ]; then
        echo "Boundary stage skipped: no osm/ohm namespaces selected"
        LAST_SUBMITTED_JOBID="$DEPENDENCY_JOBID"
        return 0
    fi

    local ARRAY_END=$((TASK_COUNT - 1))
    local BOUNDARY_SCRIPT
    BOUNDARY_SCRIPT=$(mktemp /tmp/es-boundary-stage-XXXXXX.sbatch)

    cat > "$BOUNDARY_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-boundary-stage
#SBATCH --partition=smp
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --array=0-${ARRAY_END}
#SBATCH --output=${STAGING_SLURM_LOGS}/boundary-stage-%A_%a.out
#SBATCH --error=${STAGING_SLURM_LOGS}/boundary-stage-%A_%a.err

set -e

source "$ENV_FILE"
$(activate_environment)
cd "$REPO_DIR"

NAMESPACE=$(python3 - <<PY
import json
from pathlib import Path
manifest = json.loads(Path("$RUN_MANIFEST").read_text(encoding="utf-8"))
targets = [ns for ns in manifest.get("selected_namespaces", []) if ns in ("osm", "ohm")]
idx = int("${SLURM_ARRAY_TASK_ID}")
print(targets[idx])
PY
)

python -u -m processing.boundary_stage --run-id "$RUN_ID" --namespace "$NAMESPACE" --manifest-path "$RUN_MANIFEST"
SBATCH_EOF

    local SBATCH_ARGS=(--parsable)
    if [ -n "$DEPENDENCY_JOBID" ]; then
        SBATCH_ARGS+=("--dependency=afterok:${DEPENDENCY_JOBID}")
    fi

    local JOBID
    JOBID=$(sbatch "${SBATCH_ARGS[@]}" "$BOUNDARY_SCRIPT")
    rm "$BOUNDARY_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit boundary stage array"
        return 1
    fi

    LAST_SUBMITTED_JOBID="$JOBID"
    echo "Submitted boundary stage array: $JOBID"
    echo "  Run ID: $RUN_ID"
    echo "  Tasks:  0-${ARRAY_END} (osm/ohm subset)"
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${STAGING_SLURM_LOGS}/boundary-stage-${JOBID}_*.out"
}


# =============================================================================
# BOUNDARY MERGE (Slurm array, staged-only, osm/ohm only)
# =============================================================================

do_boundary_merge_array() {
    local RUN_ID=""
    local DEPENDENCY_JOBID=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run-id)
                RUN_ID="$2"
                shift 2
                ;;
            --dependency)
                DEPENDENCY_JOBID="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    if [ -z "$RUN_ID" ]; then
        echo "ERROR: --run-id is required for boundary merge array"
        return 1
    fi

    local RUN_MANIFEST="${STAGED_RUNS_DIR}/${RUN_ID}.json"
    if [ ! -f "$RUN_MANIFEST" ]; then
        echo "ERROR: Run manifest not found: $RUN_MANIFEST"
        return 1
    fi

    local TASK_COUNT
    TASK_COUNT=$(python3 - <<PY
import json
from pathlib import Path
manifest = json.loads(Path("$RUN_MANIFEST").read_text(encoding="utf-8"))
selected = manifest.get("selected_namespaces", [])
targets = [ns for ns in selected if ns in ("osm", "ohm")]
print(len(targets))
PY
)

    if [ -z "$TASK_COUNT" ] || [ "$TASK_COUNT" -le 0 ]; then
        echo "Boundary merge skipped: no osm/ohm namespaces selected"
        LAST_SUBMITTED_JOBID="$DEPENDENCY_JOBID"
        return 0
    fi

    local ARRAY_END=$((TASK_COUNT - 1))
    local MERGE_SCRIPT
    MERGE_SCRIPT=$(mktemp /tmp/es-boundary-merge-XXXXXX.sbatch)

    cat > "$MERGE_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-boundary-merge
#SBATCH --partition=smp
#SBATCH --time=08:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=24G
#SBATCH --array=0-${ARRAY_END}
#SBATCH --output=${STAGING_SLURM_LOGS}/boundary-merge-%A_%a.out
#SBATCH --error=${STAGING_SLURM_LOGS}/boundary-merge-%A_%a.err

set -e

source "$ENV_FILE"
$(activate_environment)
cd "$REPO_DIR"

NAMESPACE=$(python3 - <<PY
import json
from pathlib import Path
manifest = json.loads(Path("$RUN_MANIFEST").read_text(encoding="utf-8"))
targets = [ns for ns in manifest.get("selected_namespaces", []) if ns in ("osm", "ohm")]
idx = int("${SLURM_ARRAY_TASK_ID}")
print(targets[idx])
PY
)

python -u -m processing.boundary_merge --run-id "$RUN_ID" --namespace "$NAMESPACE" --manifest-path "$RUN_MANIFEST"
SBATCH_EOF

    local SBATCH_ARGS=(--parsable)
    if [ -n "$DEPENDENCY_JOBID" ]; then
        SBATCH_ARGS+=("--dependency=afterok:${DEPENDENCY_JOBID}")
    fi

    local JOBID
    JOBID=$(sbatch "${SBATCH_ARGS[@]}" "$MERGE_SCRIPT")
    rm "$MERGE_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit boundary merge array"
        return 1
    fi

    LAST_SUBMITTED_JOBID="$JOBID"
    echo "Submitted boundary merge array: $JOBID"
    echo "  Run ID: $RUN_ID"
    echo "  Tasks:  0-${ARRAY_END} (osm/ohm subset)"
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${STAGING_SLURM_LOGS}/boundary-merge-${JOBID}_*.out"
}

# =============================================================================
# H3 STAGE (Slurm array, staged-only)
# =============================================================================

do_h3_stage_array() {
    local RUN_ID=""
    local DEPENDENCY_JOBID=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run-id)
                RUN_ID="$2"
                shift 2
                ;;
            --dependency)
                DEPENDENCY_JOBID="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    if [ -z "$RUN_ID" ]; then
        echo "ERROR: --run-id is required for -h3-stage"
        return 1
    fi

    local RUN_MANIFEST="${STAGED_RUNS_DIR}/${RUN_ID}.json"
    if [ ! -f "$RUN_MANIFEST" ]; then
        echo "ERROR: Run manifest not found: $RUN_MANIFEST"
        return 1
    fi

    local TASK_COUNT
    TASK_COUNT=$(python3 - <<PY
import json
from pathlib import Path
manifest = json.loads(Path("$RUN_MANIFEST").read_text(encoding="utf-8"))
print(len(manifest.get("selected_namespaces", [])))
PY
)

    if [ -z "$TASK_COUNT" ] || [ "$TASK_COUNT" -le 0 ]; then
        echo "ERROR: selected_namespaces is empty in $RUN_MANIFEST"
        return 1
    fi

    local ARRAY_END=$((TASK_COUNT - 1))
    local H3_SCRIPT
    H3_SCRIPT=$(mktemp /tmp/es-h3-stage-XXXXXX.sbatch)

    cat > "$H3_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=es-h3-stage
#SBATCH --partition=smp
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --array=0-${ARRAY_END}
#SBATCH --output=${STAGING_SLURM_LOGS}/h3-stage-%A_%a.out
#SBATCH --error=${STAGING_SLURM_LOGS}/h3-stage-%A_%a.err

set -e

source "$ENV_FILE"
$(activate_environment)
cd "$REPO_DIR"

python -u -m processing.h3_stage --run-id "$RUN_ID"
SBATCH_EOF

    local SBATCH_ARGS=(--parsable)
    if [ -n "$DEPENDENCY_JOBID" ]; then
        SBATCH_ARGS+=("--dependency=afterok:${DEPENDENCY_JOBID}")
    fi

    local H3_JOBID
    H3_JOBID=$(sbatch "${SBATCH_ARGS[@]}" "$H3_SCRIPT")
    rm "$H3_SCRIPT"

    if [ -z "$H3_JOBID" ]; then
        echo "ERROR: Failed to submit H3 stage array"
        return 1
    fi

    LAST_SUBMITTED_JOBID="$H3_JOBID"

    echo "Submitted H3 stage array job: $H3_JOBID"
    echo "  Run ID: $RUN_ID"
    echo "  Tasks:  0-${ARRAY_END}"
    echo "Monitor with:"
    echo "  squeue -j $H3_JOBID"
    echo "  tail -f ${STAGING_SLURM_LOGS}/h3-stage-${H3_JOBID}_*.out"
}

# ==============================================================================
# BOUNDARY PASS (Slurm) — manual repair/replay of OSM/OHM boundary geometry
# ==============================================================================

do_boundary_pass() {
    # Usage: es -boundary-pass [OPTIONS]
    # Manual-only: normal `es -ingest` now runs OSM/OHM geometry completion
    # inside `osm-places.py` / `ohm-places.py`.
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
    echo "  tail -f ${STAGING_SLURM_LOGS}/boundary-pass-${JOBID}.*"
}


# ==============================================================================
# GENERATE BOUNDARY TILES (Slurm)
# ==============================================================================

do_generate_tiles() {
    # Usage: es -generate-tiles [--bucket BUCKET]... [--run-id RUN_ID] [--deploy]
    # Submits a Slurm array job (one task per tile bucket) reading from
    # staged snapshots + the geom store; no Elasticsearch dependency.
    # Fixed buckets: osm, ohm, osm_misc, po, clio, nl.
    # Plus per-namespace and per-WHG-dataset buckets resolved at submit time.

    local RUN_ID=""
    local BUCKETS_ARGS=""
    local DEPLOY_ARG=""
    local DEPEND_ARG=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --run-id)
                RUN_ID="$2"
                shift 2
                ;;
            --bucket|-b)
                BUCKETS_ARGS="$BUCKETS_ARGS --bucket $2"
                shift 2
                ;;
            --depend-on)
                DEPEND_ARG="--depend-on $2"
                shift 2
                ;;
            --deploy)
                DEPLOY_ARG="--deploy"
                shift
                ;;
            --dry-run)
                DRY_RUN_ARG="--dry-run"
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    if [ -z "$RUN_ID" ]; then
        echo "ERROR: --run-id is required for staged tile generation"
        return 1
    fi

    echo "Submitting bucket-driven tile generation array for run_id: $RUN_ID"

    # --deploy is intentionally a post-array step (the array tasks each own
    # one bucket; deployment is a single rsync of all completed mbtiles).
    # Run `python -m processing.generate_tiles --deploy --skip-tippecanoe`
    # after the array completes if you want to push results.
    if [ -n "$DEPLOY_ARG" ]; then
        echo "Note: --deploy is a post-array step; ignoring here."
    fi

    python -u -m processing.submit_tiles_slurm \
        --run-id "$RUN_ID" \
        $DEPEND_ARG \
        ${DRY_RUN_ARG:-} \
        $BUCKETS_ARGS
}
# Note: do_augment_ccodes was retired with the ES-coupled
# processing/augment_ccodes.py. CCode enrichment is now done staged via
# processing/ccode_enrichment.py, driven by processing/submit_ccode_slurm.py
# in the same Slurm fan-out as h3_merge / ccode_merge.
