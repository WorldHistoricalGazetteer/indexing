#!/bin/bash
# =============================================================================
# /ix1/whcdh/elastic/scripts/es.sh
# WHG Elasticsearch and Kibana management wrapper
# =============================================================================

set -e

# --- Bootstrap: minimal hardcoded path for initial install ---
IX1_BASE="/ix1/whcdh"

# --- Load Environment Variables (if available) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# Ensure PATH includes Java if available
if [ -n "$JAVA_HOME" ] && [ -d "$JAVA_HOME/bin" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi

activate_environment() {
    cat <<'EOF'
# --- ENV SETUP ---
CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
if [ -f "$CONDA_SETUP" ]; then
    source "$CONDA_SETUP"
else
    export PATH="/ihome/whcdh/stg135/miniconda3/bin:$PATH"
fi

conda activate whg
export PYTHONPATH="/ix1/whcdh/elastic:${PYTHONPATH}"

echo "Environment: $(conda info --envs | grep '*' | awk '{print $1}')"
echo "Python: $(which python)"
echo "------------------------------------------------"
EOF
}

# =============================================================================
# INSTALLATION AND UPDATE
# =============================================================================

do_install() {
    echo "=========================================="
    echo "WHG ELASTICSEARCH INSTALLATION"
    echo "=========================================="
    echo

    # Check if already installed
    if [ -d "$ES_HOME" ] && [ -d "$KIBANA_HOME" ]; then
        echo "Elasticsearch and Kibana appear to be already installed."
        echo "  ES_HOME: $ES_HOME"
        echo "  KIBANA_HOME: $KIBANA_HOME"
        read -p "Reinstall? (y/n): " confirm
        if [ "$confirm" != "y" ]; then
            echo "Cancelled."
            return 0
        fi
    fi

    # Create directory structure
    echo "Creating directory structure..."
    mkdir -p "$IX1_BASE"/{es/{logs,snapshots/staging,snapshots/backup,config},kibana/{data,logs},esinfo,data/authorities}
    mkdir -p "$IX3_BASE/es/data"

    cd "$IX1_BASE"

    # Download and install Elasticsearch
    echo
    echo "Downloading Elasticsearch ${ES_VERSION}..."
    curl -L -O "$ES_DOWNLOAD_URL"

    echo "Extracting Elasticsearch..."
    tar xf "elasticsearch-${ES_VERSION}-linux-x86_64.tar.gz"
    rm -rf "$ES_HOME"
    mv "elasticsearch-${ES_VERSION}" "$ES_HOME"
    rm "elasticsearch-${ES_VERSION}-linux-x86_64.tar.gz"
    echo "✓ Elasticsearch installed to $ES_HOME"

    # Download and install Kibana
    echo
    echo "Downloading Kibana ${KIBANA_VERSION}..."
    curl -L -O "$KIBANA_DOWNLOAD_URL"

    echo "Extracting Kibana..."
    tar xf "kibana-${KIBANA_VERSION}-linux-x86_64.tar.gz"
    rm -rf "$KIBANA_HOME"
    mv "kibana-${KIBANA_VERSION}" "$KIBANA_HOME"
    rm "kibana-${KIBANA_VERSION}-linux-x86_64.tar.gz"
    echo "✓ Kibana installed to $KIBANA_HOME"

    # Make wrapper script executable and commit
    echo
    echo "Setting up wrapper script..."
    chmod +x "$REPO_DIR/scripts/es.sh"

    cd "$REPO_DIR"
    if git diff --quiet scripts/es.sh 2>/dev/null; then
        echo "  Script permissions already tracked"
    else
        git add scripts/es.sh
        git commit -m "Make es.sh executable" 2>/dev/null || true
        git push origin main 2>/dev/null || echo "  (Could not push - you may need to push manually)"
    fi

    # Add alias to .bashrc
    echo
    ALIAS_LINE="alias es='$REPO_DIR/scripts/es.sh'"
    if grep -q "alias es=" ~/.bashrc 2>/dev/null; then
        echo "Alias 'es' already exists in ~/.bashrc"
    else
        echo "$ALIAS_LINE" >> ~/.bashrc
        echo "✓ Added alias to ~/.bashrc"
    fi

    echo
    echo "=========================================="
    echo "INSTALLATION COMPLETE"
    echo "=========================================="
    echo
    echo "To activate the 'es' alias in your current shell:"
    echo "  source ~/.bashrc"
    echo
    echo "Then start services with:"
    echo "  es -start"
    echo
}

do_update() {
    echo "Updating from git repository..."

    if [ ! -d "$REPO_DIR" ]; then
        echo "ERROR: Repository not found at $REPO_DIR"
        echo "Run installation first or clone manually."
        return 1
    fi

    cd "$REPO_DIR"

    # Check for local changes
    if ! git diff --quiet 2>/dev/null; then
        echo "WARNING: You have local changes:"
        git status --short
        echo
        read -p "Stash changes and continue? (y/n): " confirm
        if [ "$confirm" = "y" ]; then
            git stash
            echo "Changes stashed. Restore later with: git stash pop"
        else
            echo "Cancelled."
            return 1
        fi
    fi

    echo "Pulling latest from main..."
    git pull origin main

    echo "✓ Update complete"
}

# =============================================================================
# HEALTH CHECKS
# =============================================================================

health_production() {
    echo "=========================================="
    echo "PRODUCTION HEALTH CHECK"
    echo "=========================================="
    echo

    # Check if ES is running
    if [ -f "$PROD_ES_PID" ] && kill -0 $(cat "$PROD_ES_PID") 2>/dev/null; then
        echo "Elasticsearch: RUNNING (PID: $(cat $PROD_ES_PID))"
    else
        echo "Elasticsearch: STOPPED"
        return 1
    fi

    # Check if Kibana is running
    if [ -f "$KIBANA_PID" ] && kill -0 $(cat "$KIBANA_PID") 2>/dev/null; then
        echo "Kibana: RUNNING (PID: $(cat $KIBANA_PID))"
    else
        echo "Kibana: STOPPED"
    fi

    echo

    # Cluster health
    echo "--- Cluster Health ---"
    curl -s "$PROD_ES_URL/_cluster/health?pretty" 2>/dev/null || echo "Could not connect to ES"

    echo
    echo "--- Index Summary ---"
    curl -s "$PROD_ES_URL/_cat/indices?v&h=index,health,status,docs.count,store.size" 2>/dev/null || echo "Could not connect to ES"

    echo
    echo "--- Disk Usage ---"
    echo "Production data (ix3):"
    du -sh "$PROD_DATA_DIR" 2>/dev/null || echo "  Directory not found"
    echo "Snapshots (ix1):"
    du -sh "$SNAPSHOT_DIR" 2>/dev/null || echo "  Directory not found"

    echo
    echo "--- Memory ---"
    free -h | head -2
}

health_staging() {
    echo "=========================================="
    echo "STAGING HEALTH CHECK"
    echo "=========================================="
    echo

    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "Staging instance: NOT RUNNING"
        echo
        echo "Start with: source es.sh -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    echo "Staging instance: RUNNING"
    echo "  Node: $ES_NODE"
    echo "  Port: $ES_PORT"
    echo "  Job:  $SLURM_JOB_ID"
    echo

    # Check job status
    echo "--- Slurm Job Status ---"
    squeue -j "$SLURM_JOB_ID" 2>/dev/null || echo "Job not found in queue"

    echo
    echo "--- Cluster Health ---"
    curl -s "http://${ES_NODE}:${ES_PORT}/_cluster/health?pretty" 2>/dev/null || echo "Could not connect to ES"

    echo
    echo "--- Index Summary ---"
    curl -s "http://${ES_NODE}:${ES_PORT}/_cat/indices?v&h=index,health,status,docs.count,store.size" 2>/dev/null || echo "Could not connect to ES"

    echo
    echo "--- Snapshots ---"
    SNAP_COUNT=$(curl -s "http://${ES_NODE}:${ES_PORT}/_snapshot/$STAGING_REPO_NAME/_all" 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('snapshots',[])))" 2>/dev/null || echo "?")
    echo "Snapshots in staging repo: $SNAP_COUNT"
}

# =============================================================================
# PRODUCTION ELASTICSEARCH (VM)
# =============================================================================

start_prod_es() {
    if [ -f "$PROD_ES_PID" ] && kill -0 $(cat "$PROD_ES_PID") 2>/dev/null; then
        echo "Production Elasticsearch already running (PID: $(cat $PROD_ES_PID))"
        return 0
    fi

    echo "Starting production Elasticsearch..."

    # Ensure directories exist
    mkdir -p "$PROD_DATA_DIR" "$PROD_LOG_DIR"

    # JVM heap: 15g of 32g RAM (leaves ~15g for filesystem cache)
    export ES_JAVA_OPTS="-Xms15g -Xmx15g"

    nohup "$ES_HOME/bin/elasticsearch" \
        -E cluster.name="$PROD_CLUSTER_NAME" \
        -E node.name="$PROD_NODE_NAME" \
        -E path.data="$PROD_DATA_DIR" \
        -E path.logs="$PROD_LOG_DIR" \
        -E path.repo="$SNAPSHOT_DIR" \
        -E discovery.type=single-node \
        -E xpack.security.enabled=false \
        -E network.host="$PROD_ES_HOST" \
        -E http.port="$PROD_ES_PORT" \
        > "$PROD_LOG_DIR/nohup.out" 2>&1 &

    echo $! > "$PROD_ES_PID"
    echo "Elasticsearch started (PID: $(cat $PROD_ES_PID))"

    # Wait for startup
    echo -n "Waiting for Elasticsearch..."
    for i in {1..30}; do
        if curl -s "$PROD_ES_URL/_cluster/health" > /dev/null 2>&1; then
            echo " ready!"
            return 0
        fi
        echo -n "."
        sleep 2
    done
    echo " timeout (may still be starting)"
}

stop_prod_es() {
    if [ -f "$PROD_ES_PID" ]; then
        local pid=$(cat "$PROD_ES_PID")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping Elasticsearch (PID: $pid)..."
            kill "$pid"
            sleep 5
            if kill -0 "$pid" 2>/dev/null; then
                echo "Force killing..."
                kill -9 "$pid" 2>/dev/null
            fi
        fi
        rm -f "$PROD_ES_PID"
        echo "Elasticsearch stopped."
    else
        echo "Elasticsearch is not running (no PID file)."
    fi
}

# =============================================================================
# KIBANA (VM)
# =============================================================================

start_kibana() {
    if [ -f "$KIBANA_PID" ] && kill -0 $(cat "$KIBANA_PID") 2>/dev/null; then
        echo "Kibana already running (PID: $(cat $KIBANA_PID))"
        return 0
    fi

    echo "Waiting for Elasticsearch to be ready..."
    for i in {1..30}; do
        if curl -s "$PROD_ES_URL" > /dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    if ! curl -s "$PROD_ES_URL" > /dev/null 2>&1; then
        echo "ERROR: Elasticsearch not available at $PROD_ES_URL"
        return 1
    fi

    echo "Starting Kibana (this may take 2-5 minutes to initialize)..."

    mkdir -p "${IX1_BASE}/kibana/data" "${IX1_BASE}/kibana/logs"

    nohup "$KIBANA_HOME/bin/kibana" \
        --path.data="${IX1_BASE}/kibana/data" \
        > "${IX1_BASE}/kibana/logs/nohup.out" 2>&1 &

    echo $! > "$KIBANA_PID"
    echo "Kibana started (PID: $(cat $KIBANA_PID))"
    echo "Access at: http://${PROD_ES_HOST}:5601 (wait a few minutes)"
}

stop_kibana() {
    if [ -f "$KIBANA_PID" ]; then
        local pid=$(cat "$KIBANA_PID")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping Kibana (PID: $pid)..."
            kill "$pid"
            sleep 3
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null
            fi
        fi
        rm -f "$KIBANA_PID"
        echo "Kibana stopped."
    else
        echo "Kibana is not running (no PID file)."
    fi
}

# =============================================================================
# STAGING ELASTICSEARCH (Slurm)
# =============================================================================

staging_start() {
    # Parse arguments
    local PLACES_ONLY=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --places-only)
                PLACES_ONLY=true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    # Check if staging already running
    if [ -f "$STAGING_INFO_FILE" ]; then
        source "$STAGING_INFO_FILE"
        echo "Staging instance may already be running:"
        echo "  Job ID: $SLURM_JOB_ID"
        echo "  Node:   $ES_NODE"
        echo "  Port:   $ES_PORT"
        echo

        # Verify job is actually running
        if squeue -j "$SLURM_JOB_ID" &>/dev/null 2>&1; then
            echo "Job is active. Use -staging-stop first if you want to restart."
            export ES_NODE ES_PORT ES_DATA SLURM_JOB_ID
            return 0
        else
            echo "Stale info file found. Cleaning up..."
            rm -f "$STAGING_INFO_FILE"
        fi
    fi

    STAGING_SCRIPT="${SCRIPT_DIR}/../processing/es_staging.sbatch"

    if [ ! -f "$STAGING_SCRIPT" ]; then
        echo "ERROR: Staging script not found: $STAGING_SCRIPT"
        return 1
    fi

    echo "Launching staging Elasticsearch on Slurm..."
    if $PLACES_ONLY; then
        echo "  Mode: places-only (toponyms will be rebuilt separately)"
    fi

    # Ensure log directory exists
    mkdir -p "$STAGING_SLURM_LOGS"

    # Pass places-only flag via sbatch --export
    if $PLACES_ONLY; then
        JOBID=$(sbatch --parsable --export=ALL,RESTORE_PLACES_ONLY=1 "$STAGING_SCRIPT")
    else
        JOBID=$(sbatch --parsable "$STAGING_SCRIPT")
    fi

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit Slurm job"
        return 1
    fi

    echo "Submitted job: $JOBID"
    squeue -j "$JOBID"

    echo -n "Waiting for ES to be ready..."
    for i in {1..120}; do
        if [ -f "$STAGING_INFO_FILE" ]; then
            source "$STAGING_INFO_FILE"
            # Verify ES is responding
            if curl -s --connect-timeout 5 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
                echo " ready!"
                break
            fi
        fi

        # Check if job failed
        if ! squeue -j "$JOBID" &>/dev/null 2>&1; then
            echo
            echo "ERROR: Job $JOBID is no longer running"
            echo "Check logs: ${STAGING_SLURM_LOGS}/slurm-${JOBID}.out"
            return 1
        fi

        echo -n "."
        sleep 5
    done

    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo
        echo "ERROR: Staging ES did not start within timeout"
        echo "Check logs: ${STAGING_SLURM_LOGS}/slurm-${JOBID}.out"
        return 1
    fi

    source "$STAGING_INFO_FILE"
    export ES_NODE ES_PORT ES_DATA SLURM_JOB_ID

    echo
    echo "=========================================="
    echo "STAGING ES READY"
    echo "=========================================="
    echo "  URL:  http://${ES_NODE}:${ES_PORT}"
    echo "  Node: $ES_NODE"
    echo "  Port: $ES_PORT"
    echo "  Data: $ES_DATA"
    echo "  Job:  $SLURM_JOB_ID"
    echo
    echo "Environment variables exported to current shell."
    echo "For other shells: source $STAGING_INFO_FILE"
}

staging_stop() {
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "No staging instance found (no info file at $STAGING_INFO_FILE)"
        return 0
    fi

    source "$STAGING_INFO_FILE"

    echo "=========================================="
    echo "STOPPING STAGING ES"
    echo "=========================================="
    echo
    echo "WARNING: Any unsaved work will be lost!"
    echo "Make sure you have created snapshots of your data."
    echo
    read -p "Continue? (y/n): " confirm
    if [ "$confirm" != "y" ]; then
        echo "Cancelled."
        return 0
    fi

    echo "Stopping job $SLURM_JOB_ID..."
    scancel "$SLURM_JOB_ID" 2>/dev/null || true

    # Wait for cleanup
    sleep 5
    rm -f "$STAGING_INFO_FILE"

    unset ES_NODE ES_PORT ES_DATA SLURM_JOB_ID

    echo "Staging instance stopped."
}

staging_status() {
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "No staging instance running."
        echo "Start one with: source $0 -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    echo "Staging Elasticsearch Status"
    echo "=========================================="
    echo "  Node: $ES_NODE"
    echo "  Port: $ES_PORT"
    echo "  Data: $ES_DATA"
    echo "  Job:  $SLURM_JOB_ID"
    echo
    echo "Job status:"
    squeue -j "$SLURM_JOB_ID" 2>/dev/null || echo "  Job not found in queue"
    echo
    echo "Index counts:"
    curl -s "http://${ES_NODE}:${ES_PORT}/_cat/indices?v" 2>/dev/null || echo "  Could not connect"
}

staging_logs() {
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "No staging instance found."
        echo "Recent log files:"
        ls -lt "${STAGING_SLURM_LOGS}/"*.out 2>/dev/null | head -5
        return 1
    fi

    source "$STAGING_INFO_FILE"

    LOG_OUT="${STAGING_SLURM_LOGS}/slurm-${SLURM_JOB_ID}.out"
    LOG_ERR="${STAGING_SLURM_LOGS}/slurm-${SLURM_JOB_ID}.err"

    echo "=== STDOUT (${LOG_OUT}) ==="
    tail -50 "$LOG_OUT" 2>/dev/null || echo "No stdout log found"
    echo
    echo "=== STDERR (${LOG_ERR}) ==="
    tail -50 "$LOG_ERR" 2>/dev/null || echo "No stderr log found"
}

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

# NOTE: --exclusive ensures dedicated disk I/O.
# NOTE: 16 CPUS: 1 for Osmium main loop, 8-10 for ES parallel_bulk threads, rest for GC/OS overhead.
# NOTE: --mem=120G leaves room for OS overhead on 128G nodes.
# NOTE: --signal gives the Python script 2 minutes to save state before timeout.

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
source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
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
# TOPONYM INDEX REBUILD + TRAINING DATA EXTRACTION (Consolidated Slurm job)
# ==============================================================================

do_rebuild_toponyms() {
    # Usage: source es.sh -rebuild-toponyms [VERSION] [OPTIONS...]
    # Options are passed through to rebuild_toponyms_index.py

    DATA_VERSION=${1:-3}
    shift 2>/dev/null || true  # Remove version from remaining args

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

    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}"
    mkdir -p "$LOG_DIR"

    # Output directory for training data
    OUTPUT_DIR="/ix1/whcdh/models/phonetic/data/v${DATA_VERSION}"
    SQLITE_PATH="${OUTPUT_DIR}/toponyms.db"

    # Setup local scratch (CRC convention)
    SCRATCH_VAR="/scratch/slurm-\${SLURM_JOB_ID}"

    # Capture extra args (e.g., --limit 1000)
    PYTHON_ARGS="$@"

    echo "Submitting consolidated rebuild + training extraction job..."
    echo "  Data Version: v${DATA_VERSION}"
    echo "  Output Dir:   ${OUTPUT_DIR}"
    echo "  ES Host:      http://${ES_NODE}:${ES_PORT}"
    echo "  Extra Args:   ${PYTHON_ARGS:-none}"
    echo
    echo "This job will:"
    echo "  1. Extract toponyms from places index (with attestations)"
    echo "  2. Filter pre-romanized forms (lang-script mismatches)"
    echo "  3. Generate vocabulary (expanded Unicode ranges)"
    echo "  4. Export training data to Parquet (with IPA/PanPhon features)"
    echo
    echo "Note: ES toponyms index is NOT populated until embeddings are computed."
    echo "      Use -update-embeddings to create the index with embeddings."

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-rebuild-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/rebuild_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/rebuild_v${DATA_VERSION}_%j.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

set -e

# Load Environment
source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
conda activate whg

cd "$REPO_DIR"

# Setup scratch
SCRATCH_DIR="$SCRATCH_VAR"
mkdir -p "\$SCRATCH_DIR"
mkdir -p "$OUTPUT_DIR"

echo "=============================================="
echo "WHG TOPONYMS REBUILD + TRAINING DATA EXPORT"
echo "=============================================="
echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Scratch: \$SCRATCH_DIR"
echo "Output:  $OUTPUT_DIR"
echo

# Run the consolidated rebuild script
# Skip ES indexing - toponyms index will be created when embeddings are ready
python -m phonetics.extraction.rebuild_toponyms_index \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --sqlite-path "${SQLITE_PATH}" \
    --output-dir "${OUTPUT_DIR}" \
    --scratch-dir "\$SCRATCH_DIR" \
    --training-namespaces gn wd tgn \
    --train-ratio 0.8 \
    --val-ratio 0.1 \
    --skip-es-index \
    --confirm \
    $PYTHON_ARGS

echo
echo "=============================================="
echo "JOB COMPLETE"
echo "=============================================="
echo "Output directory: $OUTPUT_DIR"
echo "  - vocab/           Character, language, script vocabularies"
echo "  - training/        Parquet files with IPA/features"
echo "  - splits/          Train/val/test ID lists"
echo "  - toponyms.db      SQLite checkpoint"
echo
echo "Next steps:"
echo "  1. Generate pairs:      source es.sh -generate-pairs $DATA_VERSION"
echo "  2. Train model:         source es.sh -train-model $DATA_VERSION"
echo "  3. Update embeddings:   source es.sh -update-embeddings $DATA_VERSION"
echo
echo "Job Finished: \$(date)"
EOF
)

    echo "✓ Rebuild job submitted: $JOBID"
    echo "  Monitor: squeue -j $JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/rebuild_v${DATA_VERSION}_${JOBID}.out"
}

# ==============================================================================
# TRAINING PAIR/TRIPLET GENERATION (Uses SQLite - no ES required)
# ==============================================================================

do_generate_pairs() {
    # Usage: source es.sh -generate-pairs VERSION
    # Generates pairs and triplets from SQLite database (toponym_attestations table)

    DATA_VERSION=${1:-3}

    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}"
    mkdir -p "$LOG_DIR"

    # Output directory for training data
    DATA_DIR="/ix1/whcdh/models/phonetic/data/v${DATA_VERSION}"

    # Check that SQLite database exists
    if [ ! -f "${DATA_DIR}/toponyms.db" ]; then
        echo "ERROR: SQLite database not found at ${DATA_DIR}/toponyms.db"
        echo "Run -rebuild-toponyms first."
        return 1
    fi

    echo "Submitting pair/triplet generation job..."
    echo "  Data Version: v${DATA_VERSION}"
    echo "  Data Dir:     ${DATA_DIR}"
    echo "  SQLite DB:    ${DATA_DIR}/toponyms.db"
    echo
    echo "This job will:"
    echo "  1. Load toponyms from SQLite database"
    echo "  2. Scan toponym_attestations for co-located toponyms"
    echo "  3. Generate positive pairs (phonetic similarity >= 0.35)"
    echo "  4. Generate Phase 1 triplets (random negatives)"
    echo "  5. Generate Phase 3 triplets (hard negatives)"
    echo

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-pairs-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/pairs_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/pairs_v${DATA_VERSION}_%j.err
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

set -e

# Load Environment
source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
conda activate whg

cd "$REPO_DIR"

# Use fast local scratch disk
SCRATCH="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\${SCRATCH}"

# Calculate workers (leave 2 cores for system)
NUM_WORKERS=\$((SLURM_CPUS_PER_TASK - 2))
if [ "\$NUM_WORKERS" -lt 1 ]; then
    NUM_WORKERS=1
fi

echo "=============================================="
echo "PAIR/TRIPLET GENERATION"
echo "=============================================="
echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Data Dir: $DATA_DIR"
echo "Workers: \$NUM_WORKERS"
echo "Scratch Dir: \${SCRATCH}"
echo

# Copy SQLite database to fast local scratch
echo "Copying SQLite database to scratch..."
cp "${DATA_DIR}/toponyms.db" "\${SCRATCH}/toponyms.db"
echo "  Done (\$(ls -lh \${SCRATCH}/toponyms.db | awk '{print \$5}'))"
echo

# Run pair generation using SQLite-driven streaming with parallel similarity
# Uses scratch for staging DB, output goes to network storage
# Enrichment is ON by default for faster training data loading
echo "Running parallel pair generation with \$NUM_WORKERS workers..."

python -m phonetics.extraction.generate_pairs \
    --data-dir "\${SCRATCH}" \
    --namespaces gn wd tgn \
    --script-pair-quota 100000 \
    --scratch-dir "\${SCRATCH}" \
    --num-workers \${NUM_WORKERS}

# Copy results from scratch to permanent storage
echo
echo "Copying results to permanent storage..."

# Clear existing data to avoid mixing stale results
rm -rf "${DATA_DIR}/pairs"
rm -rf "${DATA_DIR}/triplets"
rm -f "${DATA_DIR}/pair_generation_stats.json"

mkdir -p "${DATA_DIR}/pairs"
mkdir -p "${DATA_DIR}/triplets/phase1"
mkdir -p "${DATA_DIR}/triplets/phase1_enriched"
mkdir -p "${DATA_DIR}/triplets/phase3"

rsync -av "\${SCRATCH}/pairs/" "${DATA_DIR}/pairs/"
rsync -av "\${SCRATCH}/triplets/" "${DATA_DIR}/triplets/"
# Copy stats file (saved to parent of pairs/)
cp "\${SCRATCH}/pair_generation_stats.json" "${DATA_DIR}/" 2>/dev/null || true

echo
echo "=============================================="
echo "JOB COMPLETE"
echo "=============================================="
echo "Output:"
echo "  - ${DATA_DIR}/pairs/                    Positive pairs"
echo "  - ${DATA_DIR}/triplets/phase1           Phase 1 triplets (random negatives)"
echo "  - ${DATA_DIR}/triplets/phase3           Phase 3 triplets (hard negatives)"
echo "  - ${DATA_DIR}/pair_generation_stats.json Statistics"
echo
echo "Next: source es.sh -train-model $DATA_VERSION"
echo
echo "Job Finished: \$(date)"
EOF
)

    echo "✓ Pair generation job submitted: $JOBID"
    echo "  Monitor: squeue -j $JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/pairs_v${DATA_VERSION}_${JOBID}.out"
}

# =============================================================================
# MODEL TRAINING PIPELINE (Slurm Chained Jobs)
# =============================================================================

do_train_model() {
    # Usage: source es.sh -train-model [DATA_VERSION] [START_PHASE] [TARGET_EPOCHS]
    DATA_VERSION=${1:?Data version integer required}
    START_PHASE=${2:-1}      # Default: Start at Phase 1
    TARGET_EPOCHS=$3         # Optional: If set, implies RESUME + NEW EPOCH COUNT

    # NETWORK PATHS
    DATA_DIR="/ix1/whcdh/models/phonetic/data/v${DATA_VERSION}"
    CHECKPOINT_DIR="/ix1/whcdh/models/phonetic/checkpoints/v${DATA_VERSION}"
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}/training_v${DATA_VERSION}"

    mkdir -p "$CHECKPOINT_DIR"
    mkdir -p "$LOG_DIR"

    echo "=========================================="
    echo "SUBMITTING TRAINING PIPELINE (v${DATA_VERSION})"
    echo "Config: 1x A100, 300GB RAM, 48H Limit"
    echo "=========================================="

    # Defaults
    P1_EPOCHS=50; P2_EPOCHS=50; P3_EPOCHS=30
    LAST_JOB_ID=""

    # -------------------------------------------------------------------------
    # PHASE 1: TEACHER TRAINING
    # -------------------------------------------------------------------------
    if [ "$START_PHASE" -le 1 ]; then
        P1_ARGS=""
        if [ "$START_PHASE" -eq 1 ] && [ ! -z "$TARGET_EPOCHS" ]; then
            P1_EPOCHS=$TARGET_EPOCHS
            CKPT="${CHECKPOINT_DIR}/phase1_best.pt"
            [ -f "$CKPT" ] && P1_ARGS="--resume-from $CKPT"
        fi

        # We call the function $(activate_environment) inside the heredoc
        JOB_ID_1=$(sbatch --parsable <<BATCH_SCRIPT | cut -d';' -f1
#!/bin/bash
#SBATCH --job-name=whg-train-p1-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/phase1_%j.out
#SBATCH --error=${LOG_DIR}/phase1_%j.err
#SBATCH --cluster=gpu
#SBATCH --partition=a100
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=300G

set -e
$(activate_environment)

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
echo "Staging data from ${DATA_DIR} to \${SCRATCH_ROOT}..."

mkdir -p \${SCRATCH_ROOT}/triplets
mkdir -p \${SCRATCH_ROOT}/training
mkdir -p \${SCRATCH_ROOT}/vocab

# Stage triplets (prefer enriched if available for instant loading)
(cd "${DATA_DIR}/triplets" && tar cf - phase1 phase1_enriched 2>/dev/null) | (cd \${SCRATCH_ROOT}/triplets && tar xf -)
(cd "${DATA_DIR}" && tar cf - training vocab) | (cd \${SCRATCH_ROOT} && tar xf -)

echo "Starting Phase 1 (Teacher)..."
# Force GPU isolation and unbuffered Python output
export CUDA_VISIBLE_DEVICES=0
python -u -m phonetics.training.train \
    --phase 1 \
    --data-dir "\${SCRATCH_ROOT}" \
    --output-dir "$CHECKPOINT_DIR" \
    --epochs $P1_EPOCHS \
    --batch-size 128 \
    $P1_ARGS
BATCH_SCRIPT
)
        echo "✓ Phase 1 submitted: $JOB_ID_1"
        LAST_JOB_ID=$JOB_ID_1
    else
        echo "✓ Phase 1 skipped"
        if [ ! -f "${CHECKPOINT_DIR}/phase1_best.pt" ]; then
            echo "ERROR: phase1_best.pt missing. Cannot skip Phase 1."
            return 1
        fi
    fi

    # -------------------------------------------------------------------------
    # PHASE 2: STUDENT ALIGNMENT
    # -------------------------------------------------------------------------
    if [ "$START_PHASE" -le 2 ]; then
        DEP_FLAG=""
        [ ! -z "$LAST_JOB_ID" ] && DEP_FLAG="--dependency=afterok:${LAST_JOB_ID}"

        P2_ARGS=""
        if [ "$START_PHASE" -eq 2 ] && [ ! -z "$TARGET_EPOCHS" ]; then
            P2_EPOCHS=$TARGET_EPOCHS
            CKPT="${CHECKPOINT_DIR}/phase2_best.pt"
            if [ -f "$CKPT" ]; then
                P2_ARGS="--resume-from $CKPT"
            else
                 LATEST=$(ls -v ${CHECKPOINT_DIR}/phase2_epoch*.pt 2>/dev/null | tail -n 1)
                 [ ! -z "$LATEST" ] && P2_ARGS="--resume-from $LATEST"
            fi
        fi

        JOB_ID_2=$(sbatch --parsable $DEP_FLAG <<BATCH_SCRIPT | cut -d';' -f1
#!/bin/bash
#SBATCH --job-name=whg-train-p2-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/phase2_%j.out
#SBATCH --error=${LOG_DIR}/phase2_%j.err
#SBATCH --cluster=gpu
#SBATCH --partition=a100
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=300G

set -e
$(activate_environment)

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
echo "Staging data to \${SCRATCH_ROOT}..."

mkdir -p \${SCRATCH_ROOT}
(cd "${DATA_DIR}" && tar cf - training vocab) | (cd \${SCRATCH_ROOT} && tar xf -)

echo "Starting Phase 2 (Student Alignment)..."
# Force GPU isolation and unbuffered Python output
export CUDA_VISIBLE_DEVICES=0
python -u -m phonetics.training.train \
    --phase 2 \
    --data-dir "\${SCRATCH_ROOT}" \
    --output-dir "$CHECKPOINT_DIR" \
    --teacher-checkpoint "${CHECKPOINT_DIR}/phase1_best.pt" \
    --epochs $P2_EPOCHS \
    --batch-size 128 \
    $P2_ARGS
BATCH_SCRIPT
)
        echo "✓ Phase 2 submitted: $JOB_ID_2"
        LAST_JOB_ID=$JOB_ID_2
    else
        echo "✓ Phase 2 skipped"
        if [ ! -f "${CHECKPOINT_DIR}/phase2_best.pt" ]; then
            echo "ERROR: phase2_best.pt missing. Cannot skip Phase 2."
            return 1
        fi
    fi

    # -------------------------------------------------------------------------
    # PHASE 3: FINE TUNING
    # -------------------------------------------------------------------------
    if [ "$START_PHASE" -le 3 ]; then
        DEP_FLAG=""
        [ ! -z "$LAST_JOB_ID" ] && DEP_FLAG="--dependency=afterok:${LAST_JOB_ID}"

        P3_ARGS=""
        if [ "$START_PHASE" -eq 3 ] && [ ! -z "$TARGET_EPOCHS" ]; then
            P3_EPOCHS=$TARGET_EPOCHS
            CKPT="${CHECKPOINT_DIR}/phase3_best.pt"
            if [ -f "$CKPT" ]; then
                P3_ARGS="--resume-from $CKPT"
            else
                 LATEST=$(ls -v ${CHECKPOINT_DIR}/phase3_epoch*.pt 2>/dev/null | tail -n 1)
                 [ ! -z "$LATEST" ] && P3_ARGS="--resume-from $LATEST"
            fi
        fi

        JOB_ID_3=$(sbatch --parsable $DEP_FLAG <<BATCH_SCRIPT | cut -d';' -f1
#!/bin/bash
#SBATCH --job-name=whg-train-p3-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/phase3_%j.out
#SBATCH --error=${LOG_DIR}/phase3_%j.err
#SBATCH --cluster=gpu
#SBATCH --partition=a100
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=300G

set -e
$(activate_environment)

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
echo "Staging data to \${SCRATCH_ROOT}..."

mkdir -p \${SCRATCH_ROOT}/triplets
mkdir -p \${SCRATCH_ROOT}/training
mkdir -p \${SCRATCH_ROOT}/vocab

(cd "${DATA_DIR}/triplets" && tar cf - phase3) | (cd \${SCRATCH_ROOT}/triplets && tar xf -)
(cd "${DATA_DIR}" && tar cf - training vocab) | (cd \${SCRATCH_ROOT} && tar xf -)

echo "Starting Phase 3 (Fine Tuning)..."
# Force GPU isolation and unbuffered Python output
export CUDA_VISIBLE_DEVICES=0
python -u -m phonetics.training.train \
    --phase 3 \
    --data-dir "\${SCRATCH_ROOT}" \
    --output-dir "$CHECKPOINT_DIR" \
    --student-checkpoint "${CHECKPOINT_DIR}/phase2_best.pt" \
    --epochs $P3_EPOCHS \
    --batch-size 128 \
    $P3_ARGS
BATCH_SCRIPT
)
        echo "✓ Phase 3 submitted: $JOB_ID_3"
        LAST_JOB_ID=$JOB_ID_3
    fi

    echo
    echo "Pipeline queued. Monitor: squeue -u $USER"
    echo "tail -f ${LOG_DIR}/*_${JOB_ID_1}.*"
    echo "tail -f ${LOG_DIR}/*_${JOB_ID_2}.*"
    echo "tail -f ${LOG_DIR}/*_${JOB_ID_3}.*"

    # Return the last job ID for chaining (used by -train-and-update)
    echo "$LAST_JOB_ID"
}

# =============================================================================
# FULL PIPELINE: TRAIN + UPDATE EMBEDDINGS + CREATE INDEX
# =============================================================================
#
# Runs training phases 1-3, then computes embeddings and creates the ES index.
# All jobs are chained via Slurm dependencies so they run sequentially.
#
# Usage:
#   source es.sh -train-and-update VERSION
#
# This is equivalent to running:
#   source es.sh -train-model VERSION
#   source es.sh -update-embeddings VERSION
#
# The final index job creates a snapshot when complete.

do_train_and_update() {
    local DATA_VERSION=${1:?Data version required (e.g., 3)}

    echo "=========================================="
    echo "FULL PIPELINE: TRAIN + UPDATE (v${DATA_VERSION})"
    echo "=========================================="
    echo
    echo "This will:"
    echo "  1. Train Phase 1 (Teacher)"
    echo "  2. Train Phase 2 (Student Alignment)"
    echo "  3. Train Phase 3 (Fine Tuning)"
    echo "  4. Compute embeddings (GPU)"
    echo "  5. Create ES toponyms index"
    echo "  6. Create snapshot"
    echo

    # --- PATH CONFIGURATION ---
    local BASE_DIR="/ix1/whcdh/models/phonetic"
    local DATA_DIR="${BASE_DIR}/data/v${DATA_VERSION}"
    local VOCAB_DIR="${DATA_DIR}/vocab"
    local TRAINING_DIR="${DATA_DIR}/training"
    local SQLITE_FILE="${DATA_DIR}/toponyms.db"
    local CHECKPOINT_DIR="${BASE_DIR}/checkpoints/v${DATA_VERSION}"
    local CACHE_DIR="${BASE_DIR}/inference_cache/v${DATA_VERSION}"
    local LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}"
    local TRAIN_LOG_DIR="${LOG_DIR}/training_v${DATA_VERSION}"
    local INF_LOG_DIR="${LOG_DIR}/inference_v${DATA_VERSION}"

    # Pipeline files
    local FILE_EMB="${CACHE_DIR}/toponyms_embeddings.parquet"
    local DONE_COMPUTE="${CACHE_DIR}/.done_compute"
    local DONE_PUSH="${CACHE_DIR}/.done_push_v${DATA_VERSION}"

    mkdir -p "$CACHE_DIR" "$TRAIN_LOG_DIR" "$INF_LOG_DIR"

    # Check required files exist
    if [ ! -d "$TRAINING_DIR" ]; then
        echo "ERROR: Training data not found: $TRAINING_DIR"
        echo "Run: source es.sh -rebuild-toponyms $DATA_VERSION"
        return 1
    fi
    if [ ! -f "$SQLITE_FILE" ]; then
        echo "ERROR: SQLite database not found: $SQLITE_FILE"
        echo "Run: source es.sh -rebuild-toponyms $DATA_VERSION"
        return 1
    fi
    if [ ! -d "$VOCAB_DIR" ]; then
        echo "ERROR: Vocab directory not found: $VOCAB_DIR"
        return 1
    fi

    # Check staging ES is running (needed for index step)
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: Staging ES not running. Run -staging-start first."
        return 1
    fi
    source "$STAGING_INFO_FILE"

    # Clear any previous completion markers
    rm -f "$DONE_COMPUTE" "$DONE_PUSH" "$FILE_EMB"

    # -------------------------------------------------------------------------
    # TRAINING PHASES 1-3
    # -------------------------------------------------------------------------
    echo "Submitting training jobs..."

    # Capture the last job ID from do_train_model
    TRAIN_OUTPUT=$(do_train_model "$DATA_VERSION" 2>&1)
    echo "$TRAIN_OUTPUT" | grep -v "^[0-9]*$"  # Print output except raw job ID
    LAST_TRAIN_JOB=$(echo "$TRAIN_OUTPUT" | grep "^[0-9]*$" | tail -1)

    if [ -z "$LAST_TRAIN_JOB" ]; then
        echo "ERROR: Failed to get training job ID"
        return 1
    fi

    echo
    echo "Last training job: $LAST_TRAIN_JOB"

    # -------------------------------------------------------------------------
    # COMPUTE EMBEDDINGS (depends on Phase 3)
    # -------------------------------------------------------------------------
    echo
    echo "Submitting COMPUTE job (depends on training)..."

    JOB_COMPUTE=$(sbatch --parsable --dependency=afterok:${LAST_TRAIN_JOB} <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-compute-v${DATA_VERSION}
#SBATCH --output=${INF_LOG_DIR}/1_compute_%j.out
#SBATCH --error=${INF_LOG_DIR}/1_compute_%j.err
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=300G
#SBATCH --cluster=gpu
#SBATCH --partition=a100
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1

set -e

echo "=========================================="
echo "COMPUTE EMBEDDINGS (v${DATA_VERSION})"
echo "=========================================="
echo "Started: \$(date)"

# Load environment
if [ -f "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh" ]; then
    source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
    conda activate whg
fi

cd "$REPO_DIR"

# Find best checkpoint
MODEL_CHECKPOINT=""
if [ -f "${CHECKPOINT_DIR}/phase3_best.pt" ]; then
    MODEL_CHECKPOINT="${CHECKPOINT_DIR}/phase3_best.pt"
elif [ -f "${CHECKPOINT_DIR}/phase2_best.pt" ]; then
    MODEL_CHECKPOINT="${CHECKPOINT_DIR}/phase2_best.pt"
else
    echo "ERROR: No checkpoint found"
    exit 1
fi

echo "Model: \$MODEL_CHECKPOINT"
echo "Input: $TRAINING_DIR"

SCRATCH="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH"

CUDA_LAUNCH_BLOCKING=1 python -m phonetics.inference.update_es compute \\
    --input-file "$TRAINING_DIR" \\
    --output-file "\${SCRATCH}/embeddings.parquet" \\
    --checkpoint "\$MODEL_CHECKPOINT" \\
    --vocab-dir "$VOCAB_DIR" \\
    --batch-size 2048 \\
    --device cuda

cp "\${SCRATCH}/embeddings.parquet" "$FILE_EMB"
touch "$DONE_COMPUTE"

echo "COMPUTE complete: \$(date)"
EOF
)

    echo "✓ COMPUTE job submitted: $JOB_COMPUTE (depends on $LAST_TRAIN_JOB)"

    # -------------------------------------------------------------------------
    # CREATE INDEX (depends on Compute)
    # -------------------------------------------------------------------------
    echo
    echo "Submitting INDEX job (depends on compute)..."

    JOB_INDEX=$(sbatch --parsable --dependency=afterok:${JOB_COMPUTE} <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-index-v${DATA_VERSION}
#SBATCH --output=${INF_LOG_DIR}/2_index_%j.out
#SBATCH --error=${INF_LOG_DIR}/2_index_%j.err
#SBATCH --partition=htc
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G

set -e

echo "=========================================="
echo "CREATE TOPONYMS INDEX (v${DATA_VERSION})"
echo "=========================================="
echo "Started: \$(date)"
echo "ES Host: http://${ES_NODE}:${ES_PORT}"

# Load environment
if [ -f "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh" ]; then
    source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
    conda activate whg
fi

cd "$REPO_DIR"

SCRATCH="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH"

cp "$FILE_EMB" "\${SCRATCH}/embeddings.parquet"
cp "$SQLITE_FILE" "\${SCRATCH}/toponyms.db"

# Create index + snapshot
python -m phonetics.inference.update_es index \\
    --es-host "http://${ES_NODE}:${ES_PORT}" \\
    --index toponyms \\
    --embedding-version ${DATA_VERSION} \\
    --sqlite-file "\${SCRATCH}/toponyms.db" \\
    --embeddings-file "\${SCRATCH}/embeddings.parquet" \\
    --schema-file "$REPO_DIR/schemas/toponyms.json" \\
    --batch-size 2000

touch "$DONE_PUSH"

echo "INDEX complete: \$(date)"
echo "Snapshot created: toponyms_v${DATA_VERSION}"
EOF
)

    echo "✓ INDEX job submitted: $JOB_INDEX (depends on $JOB_COMPUTE)"

    # -------------------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------------------
    echo
    echo "=========================================="
    echo "FULL PIPELINE SUBMITTED"
    echo "=========================================="
    echo
    echo "Job chain:"
    echo "  Training Phase 1 → Phase 2 → Phase 3"
    echo "  → Compute embeddings ($JOB_COMPUTE)"
    echo "  → Create ES index ($JOB_INDEX)"
    echo "  → Snapshot: toponyms_v${DATA_VERSION}"
    echo
    echo "Monitor: squeue -u \$USER"
    echo "Logs:"
    echo "  Training: tail -f ${TRAIN_LOG_DIR}/*.out"
    echo "  Inference: tail -f ${INF_LOG_DIR}/*.out"
}
#
# Usage:
#   source es.sh -update-embeddings VERSION              # Run full pipeline
#   source es.sh -update-embeddings VERSION compute      # Compute only
#   source es.sh -update-embeddings VERSION index        # Index only (requires compute done)
#   source es.sh -update-embeddings VERSION --force      # Force re-run all stages

do_update_embeddings() {
    # Usage: source es.sh -update-embeddings VERSION [STAGE] [--force]
    # STAGE: compute | index | (empty for full pipeline)

    local DATA_VERSION=""
    local STAGE="all"
    local FORCE=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force)
                FORCE=true
                shift
                ;;
            compute|index)
                STAGE="$1"
                shift
                ;;
            *)
                if [[ -z "$DATA_VERSION" ]]; then
                    DATA_VERSION="$1"
                fi
                shift
                ;;
        esac
    done

    if [[ -z "$DATA_VERSION" ]]; then
        echo "ERROR: Data version required"
        echo "Usage: source es.sh -update-embeddings VERSION [compute|index] [--force]"
        return 1
    fi

    # --- PATH CONFIGURATION ---
    local BASE_DIR="/ix1/whcdh/models/phonetic"
    local DATA_DIR="${BASE_DIR}/data/v${DATA_VERSION}"
    local VOCAB_DIR="${DATA_DIR}/vocab"
    local TRAINING_DIR="${DATA_DIR}/training"
    local SQLITE_FILE="${DATA_DIR}/toponyms.db"
    local CHECKPOINT_DIR="${BASE_DIR}/checkpoints/v${DATA_VERSION}"
    local CACHE_DIR="${BASE_DIR}/inference_cache/v${DATA_VERSION}"
    local LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}/inference_v${DATA_VERSION}"

    # Pipeline files (persistent across runs)
    local FILE_EMB="${CACHE_DIR}/toponyms_embeddings.parquet"
    local DONE_COMPUTE="${CACHE_DIR}/.done_compute"
    local DONE_PUSH="${CACHE_DIR}/.done_push_v${DATA_VERSION}"

    mkdir -p "$CACHE_DIR"
    mkdir -p "$LOG_DIR"

    # Check required files exist
    if [ ! -d "$TRAINING_DIR" ]; then
        echo "ERROR: Training data not found: $TRAINING_DIR"
        echo "Run: source es.sh -rebuild-toponyms $DATA_VERSION"
        return 1
    fi

    if [ ! -f "$SQLITE_FILE" ]; then
        echo "ERROR: SQLite database not found: $SQLITE_FILE"
        echo "Run: source es.sh -rebuild-toponyms $DATA_VERSION"
        return 1
    fi

    # --- AUTO-DETECT MODEL CHECKPOINT ---
    local MODEL_CHECKPOINT=""
    if [[ "$STAGE" == "all" || "$STAGE" == "compute" ]]; then
        if [ -f "${CHECKPOINT_DIR}/phase3_best.pt" ]; then
            MODEL_CHECKPOINT="${CHECKPOINT_DIR}/phase3_best.pt"
        elif [ -f "${CHECKPOINT_DIR}/final_model.pt" ]; then
            MODEL_CHECKPOINT="${CHECKPOINT_DIR}/final_model.pt"
        elif [ -f "${CHECKPOINT_DIR}/phase2_best.pt" ]; then
            echo "WARNING: Phase 3 model not found. Falling back to Phase 2."
            MODEL_CHECKPOINT="${CHECKPOINT_DIR}/phase2_best.pt"
        else
            echo "ERROR: No valid checkpoint found in ${CHECKPOINT_DIR}"
            echo "Expected: phase3_best.pt, final_model.pt, or phase2_best.pt"
            return 1
        fi

        # Verify vocab exists
        if [ ! -d "$VOCAB_DIR" ]; then
            echo "ERROR: Vocab directory not found: $VOCAB_DIR"
            return 1
        fi
    fi

    echo "=========================================="
    echo "INFERENCE PIPELINE (v${DATA_VERSION})"
    echo "=========================================="
    echo "Stage:        $STAGE"
    echo "Force:        $FORCE"
    echo "Training dir: $TRAINING_DIR"
    echo "Cache dir:    $CACHE_DIR"
    [[ -n "$MODEL_CHECKPOINT" ]] && echo "Model:        $MODEL_CHECKPOINT"
    echo

    # --- STATUS CHECK ---
    echo "Pipeline Status:"
    if [ -f "$DONE_COMPUTE" ]; then
        echo "  ✓ Compute: COMPLETE ($(stat -c %y "$DONE_COMPUTE" 2>/dev/null | cut -d. -f1))"
    else
        echo "  ○ Compute: PENDING"
    fi
    if [ -f "$DONE_PUSH" ]; then
        echo "  ✓ Index:   COMPLETE ($(stat -c %y "$DONE_PUSH" 2>/dev/null | cut -d. -f1))"
    else
        echo "  ○ Index:   PENDING"
    fi
    echo

    # --- FORCE MODE: CLEAR CHECKPOINTS ---
    if $FORCE; then
        echo "Force mode: Clearing pipeline checkpoints..."
        rm -f "$DONE_COMPUTE" "$DONE_PUSH"
        rm -f "$FILE_EMB"
    fi

    # =========================================================================
    # STAGE 1: COMPUTE (Training Parquet -> GPU -> Embeddings Parquet)
    # =========================================================================
    if [[ "$STAGE" == "all" || "$STAGE" == "compute" ]]; then
        if [ -f "$DONE_COMPUTE" ] && [ -f "$FILE_EMB" ]; then
            echo "COMPUTE: Already complete. Skipping. (Use --force to re-run)"
        else
            echo "Submitting COMPUTE job..."
            echo "  Input: $TRAINING_DIR (training Parquet)"

            JOB_ID_1=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-compute-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/1_compute_%j.out
#SBATCH --error=${LOG_DIR}/1_compute_%j.err
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=300G
#SBATCH --cluster=gpu
#SBATCH --partition=a100
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1

set -e

echo "=========================================="
echo "STAGE 1: COMPUTE EMBEDDINGS"
echo "=========================================="
echo "Started: \$(date)"
echo "Model: $MODEL_CHECKPOINT"
echo "Input: $TRAINING_DIR"

# Load environment
source "${REPO_DIR}/environment_setup.sh" 2>/dev/null || true
if [ -f "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh" ]; then
    source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
    conda activate whg
fi

cd "$REPO_DIR"

# Use local scratch for output speed
SCRATCH="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH"

# Run compute directly from training Parquet (no copy needed, it's read-only)
CUDA_LAUNCH_BLOCKING=1 python -m phonetics.inference.update_es compute \
    --input-file "$TRAINING_DIR" \
    --output-file "\${SCRATCH}/embeddings.parquet" \
    --checkpoint "$MODEL_CHECKPOINT" \
    --vocab-dir "$VOCAB_DIR" \
    --batch-size 2048 \
    --device cuda

echo "Copying embeddings to shared storage..."
cp "\${SCRATCH}/embeddings.parquet" "$FILE_EMB"

# Mark complete
touch "$DONE_COMPUTE"

echo "COMPUTE complete: \$(date)"
echo "Output: $FILE_EMB"
EOF
)
            echo "  ✓ COMPUTE job submitted: $JOB_ID_1"
            echo "    Log: tail -f ${LOG_DIR}/1_compute_${JOB_ID_1}.out"
        fi
    fi

    # =========================================================================
    # STAGE 2: INDEX (Create full ES index from training data + embeddings)
    # =========================================================================
    if [[ "$STAGE" == "all" || "$STAGE" == "index" ]]; then

        # --- SMART WAIT LOGIC ---
        if [ ! -f "$DONE_COMPUTE" ]; then
            echo "🔄  PREREQUISITE CHECK: Waiting for Compute stage..."
            echo "    Target: $DONE_COMPUTE"
            echo "    (Checking every 2 minutes. Ctrl+C to cancel.)"

            while [ ! -f "$DONE_COMPUTE" ]; do
                if [ -f "$FILE_EMB" ]; then
                     CURRENT_SIZE=$(du -h "$FILE_EMB" | cut -f1)
                     echo "    ... waiting. Current embedding file size: $CURRENT_SIZE"
                else
                     echo "    ... waiting. Embedding file not created yet."
                fi
                sleep 120
            done
            echo "    ✅ Compute stage confirmed complete."
        fi

        if [ -f "$DONE_PUSH" ]; then
            echo "INDEX: Already complete. Skipping. (Use --force to re-run)"
        else
            # Check staging ES is running
            if [ ! -f "$STAGING_INFO_FILE" ]; then
                echo "ERROR: Staging ES not running. Run -staging-start first."
                return 1
            fi
            source "$STAGING_INFO_FILE"

            echo "Submitting INDEX job..."

            JOB_ID_2=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-index-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/2_index_%j.out
#SBATCH --error=${LOG_DIR}/2_index_%j.err
#SBATCH --partition=htc
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G

set -e

echo "=========================================="
echo "STAGE 2: CREATE TOPONYMS INDEX"
echo "=========================================="
echo "Started: \$(date)"
echo "ES Host: http://${ES_NODE}:${ES_PORT}"
echo "SQLite DB: $SQLITE_FILE"
echo "Embeddings: $FILE_EMB"

# Load environment
source "${REPO_DIR}/environment_setup.sh" 2>/dev/null || true
if [ -f "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh" ]; then
    source "/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
    conda activate whg
fi

cd "$REPO_DIR"

if [ ! -f "$FILE_EMB" ]; then
    echo "ERROR: Embeddings file not found: $FILE_EMB"
    exit 1
fi

if [ ! -f "$SQLITE_FILE" ]; then
    echo "ERROR: SQLite database not found: $SQLITE_FILE"
    exit 1
fi

# Use local scratch for speed
SCRATCH="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH"

echo "Staging files to local scratch..."
cp "$FILE_EMB" "\${SCRATCH}/embeddings.parquet"
cp "$SQLITE_FILE" "\${SCRATCH}/toponyms.db"

# Create full index from SQLite (all toponyms) + embeddings (training subset)
python -m phonetics.inference.update_es index \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --index toponyms \
    --embedding-version ${DATA_VERSION} \
    --sqlite-file "\${SCRATCH}/toponyms.db" \
    --embeddings-file "\${SCRATCH}/embeddings.parquet" \
    --schema-file "$REPO_DIR/schemas/toponyms.json" \
    --batch-size 2000

# Mark complete
touch "$DONE_PUSH"

echo "INDEX complete: \$(date)"
EOF
)
            echo "  ✓ INDEX job submitted: $JOB_ID_2"
            echo "    Log: tail -f ${LOG_DIR}/2_index_${JOB_ID_2}.out"
        fi
    fi

    # --- SUMMARY ---
    echo
    echo "=========================================="
    echo "PIPELINE SUMMARY"
    echo "=========================================="
    echo "Monitor jobs: squeue -u \$USER"
    echo "Cache dir:    $CACHE_DIR"
    echo "Log dir:      $LOG_DIR"
    echo
    echo "To re-run failed stages:"
    echo "  source es.sh -update-embeddings $DATA_VERSION compute"
    echo "  source es.sh -update-embeddings $DATA_VERSION index"
    echo
    echo "To force full re-run:"
    echo "  source es.sh -update-embeddings $DATA_VERSION --force"
}

# =============================================================================
# MAIN
# =============================================================================

case "$1" in
    # --- Installation and Update ---
    -install)
        do_install
        ;;
    -update)
        do_update
        ;;

    # --- Health Checks ---
    -health)
        health_production
        ;;
    -staging-health)
        health_staging
        ;;

    # --- Ingestion ---
    -ingest)
        shift  # Remove -ingest from arguments
        do_ingest "$@"
        ;;
    # --- Rebuild Toponyms Index ---
    -rebuild-toponyms)
        shift  # Remove -rebuild-toponyms from arguments
        do_rebuild_toponyms "$@"
        ;;

    # --- Generate Training Pairs/Triplets ---
    -generate-pairs)
        shift
        do_generate_pairs "$@"
        ;;

    # --- Training Pipeline ---
    -train-model)
      shift  # Remove -train-model from arguments
        do_train_model "$@"
        ;;

    # --- Full Pipeline: Train + Embeddings + Index ---
    -train-and-update)
        shift
        do_train_and_update "$@"
        ;;

    # --- Inference / Embedding Pipeline ---
    -update-embeddings)
        shift  # Remove -update-embeddings from arguments
        do_update_embeddings "$@"
        ;;

    # --- Production (VM) ---
    -start)
        start_prod_es
        start_kibana
        ;;
    -stop)
        stop_kibana
        stop_prod_es
        ;;
    -restart)
        stop_kibana
        stop_prod_es
        sleep 2
        start_prod_es
        start_kibana
        ;;
    es-start)
        start_prod_es
        ;;
    es-stop)
        stop_prod_es
        ;;
    es-restart)
        stop_prod_es
        sleep 2
        start_prod_es
        ;;
    kibana-start)
        start_kibana
        ;;
    kibana-stop)
        stop_kibana
        ;;
    kibana-restart)
        stop_kibana
        sleep 2
        start_kibana
        ;;

    # --- Staging (Slurm) ---
    -staging-start)
        shift  # Remove -staging-start from arguments
        staging_start "$@"
        ;;
    -staging-stop)
        staging_stop
        ;;
    -staging-status)
        staging_status
        ;;
    -staging-logs)
        staging_logs
        ;;

    # --- Help ---
    *)
        echo "WHG Elasticsearch Management"
        echo "============================"
        echo
        echo "Usage: $0 COMMAND [OPTIONS]"
        echo
        echo "SETUP:"
        echo "  -install            Install Elasticsearch and Kibana"
        echo "  -update             Pull latest code from git"
        echo
        echo "HEALTH CHECKS:"
        echo "  -health             Production cluster health and stats"
        echo "  -staging-health     Staging cluster health and stats"
        echo
        echo "INGESTION (requires staging ES running):"
        echo "  -ingest [OPTIONS]   Submit authority ingestion job to Slurm"
        echo
        echo "  Ingestion options (passed to ingest_all_authorities.py):"
        echo "    -n, --namespaces NS   Comma-separated list: gn,wd,pl,tgn,gb,un,osm,nl,dp,iv,loc"
        echo "    --skip-existing       Skip authorities already in index"
        echo "    --check-only          Check data availability only"
        echo
        echo "  Examples:"
        echo "    $0 -ingest                        # Ingest all authorities"
        echo "    $0 -ingest -n gn,wd               # Ingest GeoNames and Wikidata only"
        echo "    $0 -ingest --skip-existing        # Skip already ingested"
        echo "    $0 -ingest --check-only           # Check what's available"
        echo
        echo "REBUILD TOPONYMS INDEX + TRAINING DATA (requires staging ES running):"
        echo "  -rebuild-toponyms VERSION [OPTIONS]"
        echo "      Consolidated job that:"
        echo "        1. Extracts toponyms from places (with attestations)"
        echo "        2. Filters pre-romanized forms (lang-script mismatches)"
        echo "        3. Generates vocabulary (full Unicode ranges)"
        echo "        4. Exports training data to Parquet (with IPA/PanPhon)"
        echo
        echo "  Options:"
        echo "    --skip-es-index          Skip ES indexing (default: skipped)"
        echo "    --skip-training-export   Skip Parquet export"
        echo "    --resume                 Resume from existing SQLite checkpoint"
        echo "    --limit N                Limit places processed (for testing)"
        echo
        echo "  Examples:"
        echo "    $0 -rebuild-toponyms 3                    # Full rebuild v3"
        echo "    $0 -rebuild-toponyms 3 --limit 10000      # Test with subset"
        echo
        echo "GENERATE TRAINING PAIRS/TRIPLETS (uses SQLite - no ES required):"
        echo "  -generate-pairs VERSION"
        echo "      Uses SQLite database to find co-located toponyms and generates:"
        echo "        - Positive pairs (phonetic similarity >= 0.35)"
        echo "        - Phase 1 triplets (random negatives for Teacher)"
        echo "        - Phase 3 triplets (hard negatives for fine-tuning)"
        echo "      Uses fast local scratch disk for SQLite queries."
        echo
        echo "MODEL TRAINING PIPELINE:"
        echo "  -train-model VERSION [PHASE]   Submit training job for model version"
        echo "                                 PHASE: 1 (Teacher), 2 (Student), 3 (Fine-tune)"
        echo
        echo "FULL PIPELINE (train + embeddings + index):"
        echo "  -train-and-update VERSION"
        echo "      Chains all jobs: Train (P1→P2→P3) → Compute embeddings → Create ES index"
        echo "      Creates snapshot 'toponyms_vN' when complete"
        echo
        echo "EMBEDDING / INDEX PIPELINE (if training already done):"
        echo "  -update-embeddings VERSION [STAGE]"
        echo "      Compute embeddings and create full toponyms index"
        echo "      STAGE: compute (GPU), index (create ES index), or omit for both"
        echo "      Use --force to re-run completed stages"
        echo
        echo "PRODUCTION (run on VM):"
        echo "  -start              Start Elasticsearch + Kibana"
        echo "  -stop               Stop Elasticsearch + Kibana"
        echo "  -restart            Restart both"
        echo "  es-start            Start Elasticsearch only"
        echo "  es-stop             Stop Elasticsearch only"
        echo "  es-restart          Restart Elasticsearch only"
        echo "  kibana-start        Start Kibana only"
        echo "  kibana-stop         Stop Kibana only"
        echo "  kibana-restart      Restart Kibana only"
        echo
        echo "STAGING (run on CRC login node, use 'source'):"
        echo "  source $0 -staging-start              Launch staging ES on Slurm"
        echo "  source $0 -staging-start --places-only  Launch with only places index"
        echo "  source $0 -staging-stop               Stop staging ES"
        echo "  source $0 -staging-status             Show status and index counts"
        echo "  source $0 -staging-logs               Show recent log output"
        echo
        echo "Typical workflow (simple):"
        echo "  1. Start staging ES:    source es.sh -staging-start --places-only"
        echo "  2. Extract + Parquet:   source es.sh -rebuild-toponyms 3"
        echo "  3. Generate pairs:      source es.sh -generate-pairs 3"
        echo "  4. Train + Index:       source es.sh -train-and-update 3"
        echo
        echo "Typical workflow (step-by-step):"
        echo "  1. Start staging ES:    source es.sh -staging-start --places-only"
        echo "  2. Extract + Parquet:   source es.sh -rebuild-toponyms 3"
        echo "  3. Generate pairs:      source es.sh -generate-pairs 3"
        echo "  4. Train model:         source es.sh -train-model 3"
        echo "  5. Create ES index:     source es.sh -update-embeddings 3"
        echo
        echo "Data directory: /ix1/whcdh/models/phonetic/data/vN/"
        echo
        echo "NOTES:"
        echo "  - Staging: one instance at a time (port $STAGING_ES_PORT)"
        echo "  - Staging: snapshots must be created explicitly"
        echo "  - Production data: $PROD_DATA_DIR"
        echo "  - Snapshots: $SNAPSHOT_DIR"
        ;;
esac
