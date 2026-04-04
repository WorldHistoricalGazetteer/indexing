#!/bin/bash
# =============================================================================
# scripts/types.sh
# WHG Type System Orchestrator
#
# Builds vocabulary files, applies AAT mappings, syncs the AAT hierarchy
# to Elasticsearch, and merges cross-vocabulary fields.
#
# Designed to run on a CRC login node, submitting Slurm jobs for steps
# that need compute time or network access.
#
# Usage:
#   bash scripts/types.sh --help
#   bash scripts/types.sh --all --es-host URL       # full pipeline
#   bash scripts/types.sh --build-vocabs             # vocabulary files only
#   bash scripts/types.sh --map                      # AAT mapping only
#   bash scripts/types.sh --sync --es-host URL       # AAT hierarchy → ES only
#   bash scripts/types.sh --merge --es-host URL      # cross-vocab merge only
#   bash scripts/types.sh --status                   # check job status
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
IX1_BASE="${IX1_BASE:-/ix1/ishi}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="${IX1_BASE}/logs"
SLURM_ACCOUNT="${SLURM_ACCOUNT:-ishi}"

# --- Load .env (provides STAGING_INFO_FILE, etc.) ---
ENV_FILE="${REPO_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

STAGING_INFO_FILE="${STAGING_INFO_FILE:-${IX1_BASE}/esinfo/es-staging.env}"

# Conda
CONDA_SETUP_PATH="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP_PATH" ]; then
    CONDA_SETUP_PATH="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
CONDA_ENV="whg"

# ---------------------------------------------------------------------------
# Slurm configuration
# ---------------------------------------------------------------------------
# Standard QOS tiers available on CRC htc partition:
#   htc-htc-s:  1 day     htc-htc-n:  3 days
#   htc-htc-l:  6 days    htc-htc-ll: 21 days (max)
#
# The type system pipeline is lightweight (downloads + API calls + ES bulk).
# The longest step is the AAT SPARQL mapping (~30 min for label matching).
# A 3-day normal QOS gives ample headroom.

SLURM_PARTITION="htc"
SLURM_QOS_SHORT="htc-htc-s"    # 1 day  — vocab builds, merge
SLURM_QOS_NORMAL="htc-htc-n"   # 3 days — AAT sync, mapping
SLURM_CPUS=4
SLURM_MEM="16G"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ES_HOST=""
DO_BUILD=false
DO_MAP=false
DO_SYNC=false
DO_MERGE=false
DO_ALL=false
DO_STATUS=false
FORCE_AAT=false
DRY_RUN=false
WAIT=false

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --all                 Run the full pipeline (build → map → sync → merge)
  --build-vocabs        Build vocabulary files (GeoNames, Wikidata, Pleiades)
  --map                 Apply AAT mappings (static + wikidata + sparql)
  --sync                Sync AAT hierarchy → ES types index
  --merge               Merge cross-vocabulary fields into ES
  --es-host URL         Elasticsearch host URL (auto-detected from staging if not given)
  --force               Force re-download of AAT dump
  --dry-run             Dry-run for sync (report only, no indexing)
  --wait                Wait for each Slurm job to complete before starting next
  --status              Show status of running types jobs
  --help                Show this help

ES host auto-detection:
  If --es-host is not given, the script reads the staging info file
  ($STAGING_INFO_FILE) written by 'es.sh -staging-start'.
  --build-vocabs works without ES (skips Wikidata; no doc counts).
  --sync and --merge always require ES.

Slurm QOS tiers (htc partition):
  htc-htc-s   max  1 day      htc-htc-n   max  3 days
  htc-htc-l   max  6 days     htc-htc-ll  max 21 days

Examples:
  $(basename "$0") --all                                  # auto-detect staging ES
  $(basename "$0") --all --es-host http://localhost:9200   # explicit host
  $(basename "$0") --build-vocabs                          # no ES needed
  $(basename "$0") --sync --force                          # re-download AAT dump
  $(basename "$0") --status
EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)         DO_ALL=true ;;
        --build-vocabs) DO_BUILD=true ;;
        --map)         DO_MAP=true ;;
        --sync)        DO_SYNC=true ;;
        --merge)       DO_MERGE=true ;;
        --es-host)     ES_HOST="$2"; shift ;;
        --force)       FORCE_AAT=true ;;
        --dry-run)     DRY_RUN=true ;;
        --wait)        WAIT=true ;;
        --status)      DO_STATUS=true ;;
        --help|-h)     usage ;;
        *)             echo "Unknown option: $1"; usage ;;
    esac
    shift
done

if $DO_ALL; then
    DO_BUILD=true
    DO_MAP=true
    DO_SYNC=true
    DO_MERGE=true
fi

if ! $DO_BUILD && ! $DO_MAP && ! $DO_SYNC && ! $DO_MERGE && ! $DO_STATUS; then
    echo "Error: No action specified. Use --help for usage."
    exit 1
fi

# ---------------------------------------------------------------------------
# Auto-detect ES host from staging info file if not provided
# ---------------------------------------------------------------------------
if [ -z "$ES_HOST" ]; then
    if [ -f "$STAGING_INFO_FILE" ]; then
        source "$STAGING_INFO_FILE"
        ES_HOST="${ES_URL:-}"
        if [ -n "$ES_HOST" ]; then
            echo "Auto-detected staging ES: $ES_HOST"
        fi
    fi
fi

# Validate ES_HOST where strictly needed (sync and merge always need it;
# build-vocabs uses it for doc counts but GeoNames/Pleiades work without)
if ($DO_SYNC || $DO_MERGE) && [ -z "$ES_HOST" ]; then
    echo "Error: --es-host is required for --sync and --merge."
    echo "       Start a staging ES first, or pass --es-host explicitly."
    echo "       (Staging info file: $STAGING_INFO_FILE)"
    exit 1
fi

# ---------------------------------------------------------------------------
# Helper: generate the conda/env preamble for Slurm scripts
# ---------------------------------------------------------------------------
slurm_preamble() {
    cat <<PREAMBLE
#!/bin/bash

# --- Conda environment ---
CONDA_SETUP="$CONDA_SETUP_PATH"
if [ -f "\$CONDA_SETUP" ]; then
    source "\$CONDA_SETUP"
else
    export PATH="$(dirname "$CONDA_SETUP_PATH")/../bin:\$PATH"
fi
conda activate $CONDA_ENV

export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH:-}"
cd "$REPO_DIR"

echo "=================================================="
echo "Job: \$SLURM_JOB_NAME  ID: \$SLURM_JOB_ID"
echo "Node: \$(hostname)  Started: \$(date)"
echo "Python: \$(which python)"
echo "=================================================="

PREAMBLE
}

# ---------------------------------------------------------------------------
# Helper: submit a Slurm job and optionally wait
# ---------------------------------------------------------------------------
submit_job() {
    local job_name="$1"
    local qos="$2"
    local time_limit="$3"
    local script_body="$4"
    local dependency="${5:-}"

    local sbatch_file
    sbatch_file=$(mktemp /tmp/types_${job_name}_XXXXXX.sbatch)

    cat > "$sbatch_file" <<SBATCH_HEADER
#!/bin/bash
#SBATCH --job-name=$job_name
#SBATCH --output=${LOG_DIR}/types_${job_name}_%j.log
#SBATCH --error=${LOG_DIR}/types_${job_name}_%j.err
#SBATCH --time=$time_limit
#SBATCH --mem=$SLURM_MEM
#SBATCH --cpus-per-task=$SLURM_CPUS
#SBATCH --partition=$SLURM_PARTITION
#SBATCH --qos=$qos
#SBATCH --account=$SLURM_ACCOUNT
SBATCH_HEADER

    if [ -n "$dependency" ]; then
        echo "#SBATCH --dependency=afterok:${dependency}" >> "$sbatch_file"
    fi

    echo "" >> "$sbatch_file"
    slurm_preamble >> "$sbatch_file"
    echo "$script_body" >> "$sbatch_file"

    echo ""
    echo "--- Submitting: $job_name (qos=$qos, time=$time_limit) ---"

    local job_id
    job_id=$(sbatch "$sbatch_file" | awk '{print $NF}')
    echo "  Job ID: $job_id"
    echo "  Log: ${LOG_DIR}/types_${job_name}_${job_id}.log"

    rm -f "$sbatch_file"

    if $WAIT && [ -n "$job_id" ]; then
        echo "  Waiting for job $job_id to complete ..."
        squeue_wait "$job_id"
    fi

    # Return job ID for dependency chaining
    echo "$job_id"
}

squeue_wait() {
    local jid="$1"
    while squeue -j "$jid" -h 2>/dev/null | grep -q "$jid"; do
        sleep 30
    done
    # Check exit status
    local state
    state=$(sacct -j "$jid" --format=State --noheader -P | head -1)
    if [ "$state" != "COMPLETED" ]; then
        echo "  WARNING: Job $jid finished with state: $state"
    else
        echo "  Job $jid completed successfully."
    fi
}

# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------
if $DO_STATUS; then
    echo "=== WHG Types Pipeline — Job Status ==="
    squeue -u "$(whoami)" -n "types_build_vocabs,types_map_static,types_map_wikidata,types_map_sparql,types_sync_aat,types_merge" \
        -o "%.10i %.20j %.8T %.10M %.6D %R" 2>/dev/null || echo "(no running jobs)"
    echo ""
    echo "Recent completed jobs:"
    sacct -u "$(whoami)" --name="types_build_vocabs,types_map_static,types_map_wikidata,types_map_sparql,types_sync_aat,types_merge" \
        --format="JobID,JobName%25,State,Elapsed,ExitCode" --starttime="$(date -d '7 days ago' +%Y-%m-%d)" 2>/dev/null \
        | head -20 || echo "(no recent jobs)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Ensure log directory exists
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"

echo "=========================================="
echo "WHG Type System Pipeline"
echo "=========================================="
echo "  Repository: $REPO_DIR"
echo "  ES host:    ${ES_HOST:-'(not set)'}"
echo "  Steps:      $(
    $DO_BUILD && echo -n 'build '
    $DO_MAP   && echo -n 'map '
    $DO_SYNC  && echo -n 'sync '
    $DO_MERGE && echo -n 'merge '
)"
echo "=========================================="

LAST_JOB_ID=""

# ---------------------------------------------------------------------------
# Step 1: Build vocabulary files
# ---------------------------------------------------------------------------
if $DO_BUILD; then
    # Build ES_HOST flags — GeoNames and Pleiades work without ES (no counts)
    ES_FLAG=""
    if [ -n "$ES_HOST" ]; then
        ES_FLAG="--es-host $ES_HOST"
    fi

    BODY=$(cat <<SCRIPT
set -e
echo "=== Step 1: Build vocabulary files ==="

echo ""
echo "--- GeoNames types ---"
python -m typesystem.build_geonames_types $ES_FLAG

echo ""
echo "--- Pleiades types ---"
python -m typesystem.build_pleiades_types $ES_FLAG
SCRIPT
)

    # Wikidata builder requires ES (aggregation on places index)
    if [ -n "$ES_HOST" ]; then
        BODY+=$(cat <<SCRIPT

echo ""
echo "--- Wikidata types ---"
python -m typesystem.build_wikidata_types --es-host $ES_HOST
SCRIPT
)
    else
        BODY+=$(cat <<'SCRIPT'

echo ""
echo "--- Wikidata types: SKIPPED (no ES host available) ---"
echo "  Run with --es-host to include Wikidata type aggregation."
SCRIPT
)
    fi

    BODY+='
echo ""
echo "=== Vocabulary builds complete ==="
'

    LAST_JOB_ID=$(submit_job "types_build_vocabs" "$SLURM_QOS_SHORT" "04:00:00" "$BODY" "$LAST_JOB_ID" | tail -1)
fi

# ---------------------------------------------------------------------------
# Step 2: Apply AAT mappings (static → wikidata → sparql)
# ---------------------------------------------------------------------------
if $DO_MAP; then
    # 2a: Static mappings (fast, no API calls)
    BODY_STATIC=$(cat <<'SCRIPT'
set -e
echo "=== Step 2a: Static AAT mappings ==="
python -m typesystem.aat_mapper static
echo "=== Static mappings complete ==="
SCRIPT
)
    LAST_JOB_ID=$(submit_job "types_map_static" "$SLURM_QOS_SHORT" "00:30:00" "$BODY_STATIC" "$LAST_JOB_ID" | tail -1)

    # 2b: Wikidata → AAT bridge (SPARQL, moderate)
    BODY_WD=$(cat <<'SCRIPT'
set -e
echo "=== Step 2b: Wikidata → AAT bridge ==="
python -m typesystem.aat_mapper wikidata
echo "=== Wikidata bridge complete ==="
SCRIPT
)
    LAST_JOB_ID=$(submit_job "types_map_wikidata" "$SLURM_QOS_SHORT" "02:00:00" "$BODY_WD" "$LAST_JOB_ID" | tail -1)

    # 2c: SPARQL label matching (slow, many API calls)
    BODY_SPARQL=$(cat <<'SCRIPT'
set -e
echo "=== Step 2c: AAT SPARQL label matching ==="
python -m typesystem.aat_mapper sparql
echo "=== SPARQL matching complete ==="
SCRIPT
)
    LAST_JOB_ID=$(submit_job "types_map_sparql" "$SLURM_QOS_NORMAL" "12:00:00" "$BODY_SPARQL" "$LAST_JOB_ID" | tail -1)
fi

# ---------------------------------------------------------------------------
# Step 3: Sync AAT hierarchy → ES types index
# ---------------------------------------------------------------------------
if $DO_SYNC; then
    FORCE_FLAG=""
    if $FORCE_AAT; then
        FORCE_FLAG="--force"
    fi
    DRY_FLAG=""
    if $DRY_RUN; then
        DRY_FLAG="--dry-run"
    fi

    BODY=$(cat <<SCRIPT
set -e
echo "=== Step 3: Sync AAT hierarchy → ES ==="
python -m typesystem.sync_aat_types --es-host $ES_HOST $FORCE_FLAG $DRY_FLAG
echo "=== AAT sync complete ==="
SCRIPT
)
    LAST_JOB_ID=$(submit_job "types_sync_aat" "$SLURM_QOS_NORMAL" "06:00:00" "$BODY" "$LAST_JOB_ID" | tail -1)
fi

# ---------------------------------------------------------------------------
# Step 4: Merge cross-vocabulary mappings into ES
# ---------------------------------------------------------------------------
if $DO_MERGE; then
    BODY=$(cat <<SCRIPT
set -e
echo "=== Step 4: Merge cross-vocabulary mappings ==="
python -m typesystem.merge_mappings --es-host $ES_HOST
echo ""
echo "--- Coverage report ---"
python -m typesystem.aat_mapper report
echo "=== Merge complete ==="
SCRIPT
)
    LAST_JOB_ID=$(submit_job "types_merge" "$SLURM_QOS_SHORT" "01:00:00" "$BODY" "$LAST_JOB_ID" | tail -1)
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "All jobs submitted."
if $WAIT; then
    echo "Pipeline ran synchronously — all steps complete."
else
    echo "Jobs are running with Slurm dependency chaining."
    echo "Use '$(basename "$0") --status' to check progress."
    echo "Logs: ${LOG_DIR}/types_*.log"
fi
echo "=========================================="

