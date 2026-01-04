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

    # Ensure log directory exists
    mkdir -p "$STAGING_SLURM_LOGS"

    JOBID=$(sbatch --parsable "$STAGING_SCRIPT")

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
#SBATCH --time=47:00:00
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
if [ -f "/ix1/whcdh/miniconda/etc/profile.d/conda.sh" ]; then
    source "/ix1/whcdh/miniconda/etc/profile.d/conda.sh"
    conda activate whg
elif [ -f "\$HOME/miniconda/etc/profile.d/conda.sh" ]; then
    source "\$HOME/miniconda/etc/profile.d/conda.sh"
    conda activate whg
fi

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
# TOPONYM INDEX REBUILD (Slurm batch job)
# ==============================================================================

do_rebuild_toponyms() {
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

    # Setup local scratch (CRC convention)
    SCRATCH_DIR="/scratch/slurm-\${SLURM_JOB_ID}"
    mkdir -p "\$SCRATCH_DIR"

    # Capture extra args (e.g., --limit 1000)
    PYTHON_ARGS="$@"

    echo "Submitting toponym index rebuild job..."
    echo "  ES Host: http://${ES_NODE}:${ES_PORT}"
    echo "  Args:    $PYTHON_ARGS"

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-rebuild-topo
#SBATCH --output=${LOG_DIR}/rebuild_%j.out
#SBATCH --error=${LOG_DIR}/rebuild_%j.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G

set -e

# Load Environment
if [ -f "/ix1/whcdh/miniconda/etc/profile.d/conda.sh" ]; then
    source "/ix1/whcdh/miniconda/etc/profile.d/conda.sh"
    conda activate whg
elif [ -f "\$HOME/miniconda/etc/profile.d/conda.sh" ]; then
    source "\$HOME/miniconda/etc/profile.d/conda.sh"
    conda activate whg
fi

cd "$REPO_DIR"

echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Using scratch: $SCRATCH_DIR"

# Run the rebuild
# Note: We hardcode --confirm here because this is an intentional manual Slurm submission
python -m phonetics.extraction.rebuild_toponyms_index \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --scratch-dir "$SCRATCH_DIR" \
    --confirm \
    --resume \
    $PYTHON_ARGS

echo "Job Finished: \$(date)"
EOF
)

    echo "✓ Rebuild job submitted: $JOBID"
    echo "  Monitor: squeue -j $JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/rebuild_${JOBID}.out"
}

# =============================================================================
# TRAINING DATA PREPARATION (Extract -> Generate Pairs)
# =============================================================================

do_train_extract() {
    # Usage: source es.sh -train-extract [VERSION]
    DATA_VERSION=${1:?Data version integer required (e.g., 2)}

    # Check staging is running
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: Staging ES not running."
        echo "Run: source $0 -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    # Verify ES connectivity
    if ! curl -s --connect-timeout 5 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to staging ES at http://${ES_NODE}:${ES_PORT}"
        return 1
    fi

    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}"
    mkdir -p "$LOG_DIR"

    # Define final destination
    FINAL_DATA_DIR="/ix1/whcdh/models/phonetic/data/v${DATA_VERSION}"

    # Setup local scratch directory variable
    SCRATCH_VAR="/scratch/slurm-\${SLURM_JOB_ID}"

    echo "Submitting training data extraction job..."
    echo "  Version: $DATA_VERSION"
    echo "  Dest:    $FINAL_DATA_DIR"
    echo "  ES Host: http://${ES_NODE}:${ES_PORT}"

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-train-extract-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/train_extract_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/train_extract_v${DATA_VERSION}_%j.err
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G

set -e

# Load Environment
if [ -f "/ix1/whcdh/miniconda/etc/profile.d/conda.sh" ]; then
    source "/ix1/whcdh/miniconda/etc/profile.d/conda.sh"
    conda activate whg
elif [ -f "\$HOME/miniconda/etc/profile.d/conda.sh" ]; then
    source "\$HOME/miniconda/etc/profile.d/conda.sh"
    conda activate whg
fi

cd "$REPO_DIR"

# Define Scratch Directory
SCRATCH_DIR="$SCRATCH_VAR"
mkdir -p "\$SCRATCH_DIR"
mkdir -p "$FINAL_DATA_DIR"

echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Work Dir (Scratch): \$SCRATCH_DIR"
echo "Final Dir: $FINAL_DATA_DIR"

# =============================================================================
# STEP 1: EXTRACT TOPONYMS (Or Resume)
# =============================================================================
echo
echo "Checking for existing data in $FINAL_DATA_DIR..."

if [ -d "$FINAL_DATA_DIR/toponyms" ] && [ -d "$FINAL_DATA_DIR/vocab" ]; then
    echo "FOUND EXISTING DATA. RESUMING..."
    echo "Copying from Network Storage -> Local Scratch"

    # Copy existing data to scratch so Step 2 can use it
    rsync -av "$FINAL_DATA_DIR/" "\$SCRATCH_DIR/"

    echo "✓ Data staged to scratch. Skipping Step 1."
else
    echo "NO EXISTING DATA FOUND. STARTING FRESH EXTRACTION..."
    echo "=========================================="
    echo "STEP 1: Extracting Toponyms"
    echo "=========================================="

    python -m phonetics.extraction.extract_to_parquet \
        --es-host "http://${ES_NODE}:${ES_PORT}" \
        --output-dir "\$SCRATCH_DIR" \
        --namespaces gn pl iv \
        --batch-size 2000

    # CHECKPOINT: Save extracted data immediately
    echo
    echo "CHECKPOINT: Saving Extracted Toponyms to Network Storage"
    rsync -av "\$SCRATCH_DIR/" "$FINAL_DATA_DIR/"
fi

# =============================================================================
# STEP 2: GENERATE PAIRS AND TRIPLETS
# =============================================================================
echo
echo "=========================================="
echo "STEP 2: Generating Pairs and Triplets"
echo "=========================================="
# Runs on data in $SCRATCH_DIR (whether newly extracted or staged from checkpoint)
python -m phonetics.extraction.generate_pairs \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --data-dir "\$SCRATCH_DIR" \
    --namespaces gn pl iv \
    --batch-size 50000

# =============================================================================
# STEP 3: FINAL SYNC
# =============================================================================
echo
echo "=========================================="
echo "STEP 3: Copying Final Data to Persistent Storage"
echo "=========================================="

# rsync again to capture the new 'triplets' folder from Step 2
rsync -av "\$SCRATCH_DIR/" "$FINAL_DATA_DIR/"

echo
echo "=========================================="
echo "PIPELINE COMPLETE"
echo "=========================================="
echo "Data available at: $FINAL_DATA_DIR"
echo "Job Finished: \$(date)"
EOF
)

    echo "✓ Training preparation job submitted: $JOBID"
    echo "  Monitor: squeue -j $JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/train_extract_v${DATA_VERSION}_${JOBID}.out"
}

# =============================================================================
# MODEL TRAINING PIPELINE (Slurm Chained Jobs)
# =============================================================================

do_train_model() {
    # Usage: source es.sh -train-model [DATA_VERSION] [START_PHASE] [TARGET_EPOCHS]
    DATA_VERSION=${1:?Data version integer required}
    START_PHASE=${2:-1}      # Default: Start at Phase 1
    TARGET_EPOCHS=$3         # Optional: If set, implies RESUME + NEW EPOCH COUNT

    # NETWORK PATHS (Permanent Storage)
    DATA_DIR="/ix1/whcdh/models/phonetic/data/v${DATA_VERSION}"
    CHECKPOINT_DIR="/ix1/whcdh/models/phonetic/checkpoints/v${DATA_VERSION}"
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}/training_v${DATA_VERSION}"

    mkdir -p "$CHECKPOINT_DIR"
    mkdir -p "$LOG_DIR"

    echo "=========================================="
    echo "SUBMITTING TRAINING PIPELINE (v${DATA_VERSION})"
    echo "Starting Phase: $START_PHASE"
    if [ ! -z "$TARGET_EPOCHS" ]; then
        echo "Resuming Phase $START_PHASE -> extending to $TARGET_EPOCHS epochs"
    fi
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

        JOB_ID_1=$(sbatch --parsable <<EOF
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
#SBATCH --mem=64G

set -e
source "${REPO_DIR}/environment_setup.sh"

# --- FAST DATA STAGING TO LOCAL SCRATCH ---
SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
echo "Staging data from ${DATA_DIR} to \${SCRATCH_ROOT}..."

mkdir -p \${SCRATCH_ROOT}/triplets
mkdir -p \${SCRATCH_ROOT}/toponyms
mkdir -p \${SCRATCH_ROOT}/vocab

# Use tar pipe for faster copy of small files
(cd "${DATA_DIR}/triplets" && tar cf - phase1) | (cd \${SCRATCH_ROOT}/triplets && tar xf -)
(cd "${DATA_DIR}" && tar cf - toponyms vocab) | (cd \${SCRATCH_ROOT} && tar xf -)

echo "Data staged. Starting Phase 1 (Teacher)..."
python -m phonetics.training.train \
    --phase 1 \
    --data-dir "\${SCRATCH_ROOT}" \
    --output-dir "$CHECKPOINT_DIR" \
    --epochs $P1_EPOCHS \
    --batch-size 128 \
    $P1_ARGS
EOF
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

        JOB_ID_2=$(sbatch --parsable $DEP_FLAG <<EOF
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
#SBATCH --mem=64G

set -e
source "${REPO_DIR}/environment_setup.sh"

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
echo "Staging data to \${SCRATCH_ROOT}..."

mkdir -p \${SCRATCH_ROOT}
# Phase 2 only needs toponyms and vocab
(cd "${DATA_DIR}" && tar cf - toponyms vocab) | (cd \${SCRATCH_ROOT} && tar xf -)

echo "Starting Phase 2 (Student Alignment)..."
python -m phonetics.training.train \
    --phase 2 \
    --data-dir "\${SCRATCH_ROOT}" \
    --output-dir "$CHECKPOINT_DIR" \
    --teacher-checkpoint "${CHECKPOINT_DIR}/phase1_best.pt" \
    --epochs $P2_EPOCHS \
    --batch-size 128 \
    $P2_ARGS
EOF
)
        echo "✓ Phase 2 submitted: $JOB_ID_2 ${DEP_FLAG}"
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

        JOB_ID_3=$(sbatch --parsable $DEP_FLAG <<EOF
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
#SBATCH --mem=64G

set -e
source "${REPO_DIR}/environment_setup.sh"

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
echo "Staging data to \${SCRATCH_ROOT}..."

mkdir -p \${SCRATCH_ROOT}/triplets
mkdir -p \${SCRATCH_ROOT}/toponyms
mkdir -p \${SCRATCH_ROOT}/vocab

# Phase 3 needs specific triplets
(cd "${DATA_DIR}/triplets" && tar cf - phase3) | (cd \${SCRATCH_ROOT}/triplets && tar xf -)
(cd "${DATA_DIR}" && tar cf - toponyms vocab) | (cd \${SCRATCH_ROOT} && tar xf -)

echo "Starting Phase 3 (Fine Tuning)..."
python -m phonetics.training.train \
    --phase 3 \
    --data-dir "\${SCRATCH_ROOT}" \
    --output-dir "$CHECKPOINT_DIR" \
    --student-checkpoint "${CHECKPOINT_DIR}/phase2_best.pt" \
    --epochs $P3_EPOCHS \
    --batch-size 128 \
    $P3_ARGS
EOF
)
        echo "✓ Phase 3 submitted: $JOB_ID_3 ${DEP_FLAG}"
    fi

    echo
    echo "Pipeline queued. Monitor: squeue -u $USER"
}

# =============================================================================
# INFERENCE PIPELINE (Extract [CPU] -> Compute [GPU] -> Push [CPU])
# =============================================================================

do_update_embeddings() {
    # Usage: source es.sh -update-embeddings [VERSION]
    # Example: source es.sh -update-embeddings 1

    DATA_VERSION=${1:?Data version integer required}

    # Check staging ES is running
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: Staging ES not running. Run -staging-start first."
        return 1
    fi
    source "$STAGING_INFO_FILE"

    # --- AUTOMATIC PATH RESOLUTION ---
    BASE_DIR="/ix1/whcdh/models/phonetic"
    VOCAB_DIR="${BASE_DIR}/data/v${DATA_VERSION}/vocab"
    CHECKPOINT_DIR="${BASE_DIR}/checkpoints/v${DATA_VERSION}"

    # Auto-detect best model checkpoint
    if [ -f "${CHECKPOINT_DIR}/phase3_best.pt" ]; then
        MODEL_CHECKPOINT="${CHECKPOINT_DIR}/phase3_best.pt"
    elif [ -f "${CHECKPOINT_DIR}/final_model.pt" ]; then
        MODEL_CHECKPOINT="${CHECKPOINT_DIR}/final_model.pt"
    elif [ -f "${CHECKPOINT_DIR}/phase2_best.pt" ]; then
        echo "WARNING: Phase 3 model not found. Falling back to Phase 2."
        MODEL_CHECKPOINT="${CHECKPOINT_DIR}/phase2_best.pt"
    else
        echo "ERROR: No valid checkpoint found in ${CHECKPOINT_DIR}"
        echo "Expected phase3_best.pt or final_model.pt"
        return 1
    fi

    # Verify Vocab
    if [ ! -d "$VOCAB_DIR" ]; then
        echo "ERROR: Vocab directory not found: $VOCAB_DIR"
        return 1
    fi

    # Generate a unique pipeline ID for logs/files
    PIPE_ID=$(date +%s)

    # PATHS
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/whcdh/es/logs}/inference_v${DATA_VERSION}_${PIPE_ID}"
    # This must be SHARED storage so jobs running on different nodes can see it
    HANDOFF_DIR="${BASE_DIR}/inference_cache/${PIPE_ID}"

    mkdir -p "$LOG_DIR"
    mkdir -p "$HANDOFF_DIR"

    FILE_RAW="${HANDOFF_DIR}/toponyms_raw.parquet"
    FILE_EMB="${HANDOFF_DIR}/toponyms_embeddings.parquet"

    echo "=========================================="
    echo "SUBMITTING INFERENCE PIPELINE (v${DATA_VERSION})"
    echo "Pipeline ID: $PIPE_ID"
    echo "Model:       $MODEL_CHECKPOINT"
    echo "Vocab:       $VOCAB_DIR"
    echo "=========================================="

    # -------------------------------------------------------------------------
    # JOB 1: EXTRACT (CPU / HTC Partition)
    # ES -> Local Scratch -> Shared Storage
    # -------------------------------------------------------------------------
    JOB_ID_1=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-1-extract
#SBATCH --output=${LOG_DIR}/1_extract_%j.out
#SBATCH --error=${LOG_DIR}/1_extract_%j.err
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --partition=htc

set -e
source "${REPO_DIR}/environment_setup.sh"

# Setup Fast Local Scratch
SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH_ROOT"
LOCAL_RAW="\${SCRATCH_ROOT}/raw.parquet"

echo "Starting Extraction to Local Scratch..."
echo "ES Host: http://${ES_NODE}:${ES_PORT}"

python -m phonetics.inference.update_es extract \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --index toponyms \
    --embedding-version $DATA_VERSION \
    --output-file "\$LOCAL_RAW" \
    --batch-size 5000 \
    --scroll-size 5000

echo "Extraction done. Moving to shared storage..."
rsync -av "\$LOCAL_RAW" "$FILE_RAW"

echo "Job 1 Complete."
EOF
)
    echo "✓ Job 1 (Extract) Submitted: $JOB_ID_1 (Partition: htc)"


    # -------------------------------------------------------------------------
    # JOB 2: COMPUTE (GPU Partition)
    # Shared Storage -> Local Scratch -> GPU -> Shared Storage
    # -------------------------------------------------------------------------
    JOB_ID_2=$(sbatch --parsable --dependency=afterok:${JOB_ID_1} <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-2-compute
#SBATCH --output=${LOG_DIR}/2_compute_%j.out
#SBATCH --error=${LOG_DIR}/2_compute_%j.err
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --partition=a100
#SBATCH --qos=gpu-a100-l
#SBATCH --gres=gpu:1

set -e
source "${REPO_DIR}/environment_setup.sh"

# Setup Fast Local Scratch
SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH_ROOT"

LOCAL_RAW="\${SCRATCH_ROOT}/raw.parquet"
LOCAL_EMB="\${SCRATCH_ROOT}/emb.parquet"

echo "Staging input data to local scratch..."
rsync -av "$FILE_RAW" "\$LOCAL_RAW"

echo "Starting Inference Phase..."
python -m phonetics.inference.update_es compute \
    --input-file "\$LOCAL_RAW" \
    --output-file "\$LOCAL_EMB" \
    --checkpoint "$MODEL_CHECKPOINT" \
    --vocab-dir "$VOCAB_DIR" \
    --batch-size 2048 \
    --device cuda

echo "Saving results back to shared storage..."
rsync -av "\$LOCAL_EMB" "$FILE_EMB"

echo "Job 2 Complete."
EOF
)
    echo "✓ Job 2 (Compute) Submitted: $JOB_ID_2 (Partition: a100, Depends on $JOB_ID_1)"


    # -------------------------------------------------------------------------
    # JOB 3: PUSH (CPU / HTC Partition)
    # Shared Storage -> Local Scratch -> Elasticsearch
    # -------------------------------------------------------------------------
    JOB_ID_3=$(sbatch --parsable --dependency=afterok:${JOB_ID_2} <<EOF
#!/bin/bash
#SBATCH --job-name=whg-inf-3-push
#SBATCH --output=${LOG_DIR}/3_push_%j.out
#SBATCH --error=${LOG_DIR}/3_push_%j.err
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --partition=htc

set -e
source "${REPO_DIR}/environment_setup.sh"

# Setup Fast Local Scratch
SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH_ROOT"
LOCAL_EMB="\${SCRATCH_ROOT}/emb.parquet"

echo "Staging embeddings to local scratch..."
rsync -av "$FILE_EMB" "\$LOCAL_EMB"

echo "Starting Push Phase..."
echo "ES Host: http://${ES_NODE}:${ES_PORT}"

python -m phonetics.inference.update_es push \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --index toponyms \
    --embedding-version $DATA_VERSION \
    --input-file "\$LOCAL_EMB" \
    --batch-size 2000

echo "Job 3 Complete. Pipeline Finished."
EOF
)
    echo "✓ Job 3 (Push) Submitted:    $JOB_ID_3 (Partition: htc, Depends on $JOB_ID_2)"
    echo
    echo "Monitor: squeue -u $USER"
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

    # --- Training Data Preparation ---
    -train-extract)
        shift  # Remove -train-extract from arguments
        do_train_extract "$@"
        ;;

    # --- Training Pipeline ---
    -train-model)
      shift  # Remove -train-model from arguments
        do_train_model "$@"
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
        staging_start
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
        echo "REBUILD TOPONYMS INDEX (requires staging ES running):"
        echo "  -rebuild-toponyms [OPTIONS]   Submit toponym index rebuild job to Slurm"
        echo "  Use --limit N to limit number of toponyms processed (for testing)"
        echo "  Use --resume if a valid database snapshot exists"
        echo
        echo "TRAINING DATA PREPARATION:"
        echo "  -train-extract VERSION        Extract toponyms and generate pairs for model version"
        echo
        echo "MODEL TRAINING PIPELINE:"
        echo "  -train-model VERSION          Submit full training pipeline for model version"
        echo
        echo "EMBEDDING UPDATE PIPELINE:"
        echo "  -update-embeddings VERSION    Submit embedding update pipeline for model version"
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
        echo "  source $0 -staging-start    Launch staging ES on Slurm"
        echo "  source $0 -staging-stop     Stop staging ES"
        echo "  source $0 -staging-status   Show status and index counts"
        echo "  source $0 -staging-logs     Show recent log output"
        echo
        echo "Workflow:"
        echo "  1. Start staging ES: source es.sh -staging-start"
        echo "  2. Extract: source es.sh -staging-embed-extract 1"
        echo "  3. Run GPU job: source es.sh -staging-embed-transform 1"
        echo "  4. Load results: source es.sh -staging-embed-load 1"
        echo
        echo "Data directory: $REPO_DIR/data/embed_pipeline/"
        echo "  - raw_chunk_NNNN.parquet    (Phase 1 output)"
        echo "  - vectors_chunk_NNNN.parquet (Phase 2 output)"
        echo
        echo "NOTES:"
        echo "  - Staging: one instance at a time (port $STAGING_ES_PORT)"
        echo "  - Staging: snapshots must be created explicitly"
        echo "  - Production data: $PROD_DATA_DIR"
        echo "  - Snapshots: $SNAPSHOT_DIR"
        ;;
esac