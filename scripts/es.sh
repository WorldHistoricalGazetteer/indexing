#!/bin/bash
# =============================================================================
# /ix1/ishi/elastic/scripts/es.sh
# WHG Elasticsearch and Kibana management wrapper
# =============================================================================

set -e

# --- Bootstrap: minimal hardcoded path for initial install ---
IX1_BASE="/ix1/ishi"

# --- Load Environment Variables (if available) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# --- Conda Environment Setup ---
# Admins are renaming 'whcdh' to 'ishi'. Fallback for transition period.
CONDA_SETUP_PATH="/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh"
if [ ! -f "$CONDA_SETUP_PATH" ]; then
    CONDA_SETUP_PATH="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"
fi
CONDA_BIN_PATH="$(dirname "$CONDA_SETUP_PATH")/../bin"

# Ensure PATH includes Java if available
if [ -n "$JAVA_HOME" ] && [ -d "$JAVA_HOME/bin" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi

activate_environment() {
    cat <<EOF
# --- ENV SETUP ---
CONDA_SETUP="$CONDA_SETUP_PATH"
if [ -f "\$CONDA_SETUP" ]; then
    source "\$CONDA_SETUP"
else
    export PATH="$CONDA_BIN_PATH:\$PATH"
fi

conda activate whg
export PYTHONPATH="/ix1/ishi/elastic:${PYTHONPATH}"

echo "Environment: $(conda info --envs | grep '*' | awk '{print $1}')"
echo "Python: $(which python)"
echo "------------------------------------------------"
EOF
}

# Helper: curl with elastic credentials when security is enabled
es_curl() {
    local ELASTIC_PASS_FILE="${IX1_BASE}/es/config/elastic.password"
    if [ -f "$ELASTIC_PASS_FILE" ] && [ -f "${SSL_CERT:-}" ]; then
        curl -s -u "elastic:$(cat "$ELASTIC_PASS_FILE")" "$@"
    else
        curl -s "$@"
    fi
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
# SECURITY SETUP (one-time, run after certbot)
# =============================================================================

do_setup_security() {
    # Usage: es -setup-security
    #
    # One-time setup after certbot has issued certificates.
    # Sets passwords for built-in ES users and saves the kibana_system
    # password so start_kibana can use it.
    #
    # Requires ES to be running WITHOUT security (es -start before certbot).
    # After this completes, restart ES to pick up security=true.

    echo "=========================================="
    echo "SECURITY SETUP"
    echo "=========================================="
    echo

    # Check ES is running
    if ! curl -s "${PROD_ES_URL}/_cluster/health" > /dev/null 2>&1; then
        echo "ERROR: ES not running at ${PROD_ES_URL}"
        echo "Start it first: es -start"
        return 1
    fi

    # Check certs exist
    if [ ! -f "${SSL_CERT:-}" ] || [ ! -f "${SSL_KEY:-}" ]; then
        echo "ERROR: TLS certificates not found."
        echo "  Expected: ${SSL_CERT}"
        echo "  Run certbot first:"
        echo "    certbot certonly --dns-digitalocean \\"
        echo "      --dns-digitalocean-credentials ~/.do-credentials.ini \\"
        echo "      -d kibana.whgazetteer.org \\"
        echo "      -d index.whgazetteer.org"
        return 1
    fi
    echo "✓ TLS certificates found: ${SSL_CERT}"
    echo

    CONFIG_DIR="${IX1_BASE}/es/config"
    mkdir -p "$CONFIG_DIR"
    KIBANA_PASS_FILE="${CONFIG_DIR}/kibana_system.password"
    ELASTIC_PASS_FILE="${CONFIG_DIR}/elastic.password"

    # Generate a strong random password for each user
    ELASTIC_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')
    KIBANA_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')

    echo "Setting 'elastic' superuser password..."
    RESULT=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
        "${PROD_ES_URL}/_security/user/elastic/_password" \
        -H 'Content-Type: application/json' \
        -d "{\"password\": \"${ELASTIC_PASSWORD}\"}")
    if [ "$RESULT" != "200" ]; then
        echo "ERROR: Failed to set elastic password (HTTP ${RESULT})"
        echo "Is xpack.security already enabled? If so, provide existing credentials."
        return 1
    fi
    echo "$ELASTIC_PASSWORD" > "$ELASTIC_PASS_FILE"
    chmod 600 "$ELASTIC_PASS_FILE"
    echo "✓ elastic password saved to ${ELASTIC_PASS_FILE}"
    echo

    echo "Setting 'kibana_system' password..."
    curl -s -o /dev/null -X POST \
        "${PROD_ES_URL}/_security/user/kibana_system/_password" \
        -u "elastic:${ELASTIC_PASSWORD}" \
        -H 'Content-Type: application/json' \
        -d "{\"password\": \"${KIBANA_PASSWORD}\"}"
    echo "$KIBANA_PASSWORD" > "$KIBANA_PASS_FILE"
    chmod 600 "$KIBANA_PASS_FILE"
    echo "✓ kibana_system password saved to ${KIBANA_PASS_FILE}"
    echo

    echo "=========================================="
    echo "SETUP COMPLETE"
    echo "=========================================="
    echo
    echo "elastic (superuser) password: ${ELASTIC_PASSWORD}"
    echo "  Saved to: ${ELASTIC_PASS_FILE}"
    echo
    echo "kibana_system password: (saved to ${KIBANA_PASS_FILE})"
    echo
    echo "Next steps:"
    echo "  1. Restart ES to enable security:  es -restart"
    echo "  2. Log in to Kibana at:            https://${KIBANA_PUBLIC_HOST:-kibana.whgazetteer.org}:5601"
    echo "     Username: elastic"
    echo "     Password: ${ELASTIC_PASSWORD}"
    echo
    echo "  Store the elastic password somewhere safe - it cannot be recovered."
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
    es_curl "$PROD_ES_URL/_cluster/health?pretty" 2>/dev/null || echo "Could not connect to ES"

    echo
    echo "--- Index Summary ---"
    es_curl "$PROD_ES_URL/_cat/indices?v&h=index,health,status,docs.count,store.size" 2>/dev/null || echo "Could not connect to ES"

    echo
    echo "--- Disk Usage ---"
    echo "Production data (vast):"
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

    # ES 9.0 jvm.options has a relative 'logs/gc.log' path which fails unless
    # the working directory has a writable logs/ subdir. Patch it in place.
    if [ -f "$ES_HOME/config/jvm.options" ]; then
        sed -i "s|file=logs/gc\.log|file=${PROD_LOG_DIR}/gc.log|g" \
            "$ES_HOME/config/jvm.options"
    fi

    # Enable security (required for Kibana auth) only when TLS certs exist.
    # Before certbot has run, keep security off so es -setup-security can reach ES.
    SECURITY_FLAG="false"
    if [ -f "${SSL_CERT:-}" ] && [ -f "${SSL_KEY:-}" ]; then
        SECURITY_FLAG="true"
        echo "  TLS certs found - starting with xpack.security enabled"
    else
        echo "  No TLS certs - starting WITHOUT security (run es -setup-security after certbot)"
    fi

    # JVM heap: 15g of 32g RAM (leaves ~15g for filesystem cache)
    export ES_JAVA_OPTS="-Xms15g -Xmx15g"
    export ES_TMPDIR="/tmp"

    nohup "$ES_HOME/bin/elasticsearch" \
        -E cluster.name="$PROD_CLUSTER_NAME" \
        -E node.name="$PROD_NODE_NAME" \
        -E path.data="$PROD_DATA_DIR" \
        -E path.logs="$PROD_LOG_DIR" \
        -E path.repo="$SNAPSHOT_DIR" \
        -E discovery.type=single-node \
        -E xpack.security.enabled="${SECURITY_FLAG}" \
        -E xpack.security.transport.ssl.enabled=false \
        -E network.host="$PROD_ES_BIND_HOST" \
        -E http.port="$PROD_ES_PORT" \
        > "$PROD_LOG_DIR/nohup.out" 2>&1 &

    echo $! > "$PROD_ES_PID"
    echo "Elasticsearch started (PID: $(cat $PROD_ES_PID))"

    # Wait for startup (large heap needs up to 3 minutes to initialise)
    echo -n "Waiting for Elasticsearch..."
    for i in {1..60}; do
        if es_curl "$PROD_ES_URL/_cluster/health" > /dev/null 2>&1; then
            echo " ready!"
            return 0
        fi
        echo -n "."
        sleep 3
    done
    echo " timeout (may still be starting - check: curl $PROD_ES_URL/_cluster/health)"
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
        if es_curl "$PROD_ES_URL" > /dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    if ! es_curl "$PROD_ES_URL" > /dev/null 2>&1; then
        echo "ERROR: Elasticsearch not available at $PROD_ES_URL"
        return 1
    fi

    echo "Starting Kibana (this may take 2-5 minutes to initialize)..."

    mkdir -p "${IX1_BASE}/kibana/data" "${IX1_BASE}/kibana/logs"

    # Build Kibana args - add SSL and auth when certs are available
    KIBANA_EXTRA_ARGS=""
    if [ -f "${SSL_CERT:-}" ] && [ -f "${SSL_KEY:-}" ]; then
        echo "  TLS certs found - Kibana will serve HTTPS on port 5601"

        # Read kibana_system password (set by es -setup-security)
        KIBANA_PASS_FILE="${IX1_BASE}/es/config/kibana_system.password"
        if [ ! -f "$KIBANA_PASS_FILE" ]; then
            echo "ERROR: Kibana password not set. Run: es -setup-security"
            return 1
        fi
        KIBANA_PASSWORD=$(cat "$KIBANA_PASS_FILE")

        KIBANA_EXTRA_ARGS="
            --server.ssl.enabled=true
            --server.ssl.certificate=${SSL_CERT}
            --server.ssl.key=${SSL_KEY}
            --server.publicBaseUrl=https://${KIBANA_PUBLIC_HOST:-kibana.whgazetteer.org}
            --elasticsearch.username=kibana_system
            --elasticsearch.password=${KIBANA_PASSWORD}"
    else
        echo "  No TLS certs - Kibana will serve HTTP (no authentication)"
        echo "  Run certbot then es -setup-security to enable HTTPS + login"
    fi

    nohup "$KIBANA_HOME/bin/kibana" \
        --server.host="0.0.0.0" \
        --path.data="${IX1_BASE}/kibana/data" \
        ${KIBANA_EXTRA_ARGS} \
        > "${IX1_BASE}/kibana/logs/nohup.out" 2>&1 &

    echo $! > "$KIBANA_PID"
    echo "Kibana started (PID: $(cat $KIBANA_PID))"
    if [ -f "${SSL_CERT:-}" ]; then
        echo "Access at: https://${KIBANA_PUBLIC_HOST:-kibana.whgazetteer.org}:5601"
    else
        echo "Access at: http://${PROD_ES_HOST}:5601 (wait a few minutes)"
    fi
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
        if squeue -j "$SLURM_JOB_ID" &>/dev/null; then
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
    echo "  ES available at http://${ES_NODE}:${ES_PORT}"
    echo "  Query Cluster Health:"
    echo "    curl -s http://${ES_NODE}:${ES_PORT}/_cluster/health?pretty"
    echo "  Check Indices:"
    echo "    curl -s http://${ES_NODE}:${ES_PORT}/_cat/indices?v"
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
# TOPONYM INDEX REBUILD WITH PANPHON EMBEDDINGS
# ==============================================================================

do_rebuild_toponyms() {
    # Usage: source es.sh -rebuild-toponyms [VERSION] [OPTIONS...]
    # Options are passed through to rebuild_toponyms_index.py

    DATA_VERSION=${1:-6}
    shift 2>/dev/null || true  # Remove version from remaining args

    # Capture extra args (e.g., --limit 1000, --resume, --skip-es-index)
    PYTHON_ARGS="$@"

    # Check staging is running (always required — rebuild reads from places
    # index and writes to toponyms index)
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

    ES_URL="http://${ES_NODE}:${ES_PORT}"

    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/logs}"
    mkdir -p "$LOG_DIR"

    # Output directory for data
    OUTPUT_DIR="/ix1/ishi/models/phonetic/data/v${DATA_VERSION}"
    DB_PATH="${IX1_BASE}/data/toponyms.db"
    NEURAL_PHONETICS="${OUTPUT_DIR}/neural_phonetics.parquet"

    # Setup local scratch (CRC convention)
    SCRATCH_VAR="/scratch/slurm-\${SLURM_JOB_ID}"

    echo "=========================================="
    echo "REBUILD TOPONYMS INDEX"
    echo "=========================================="
    echo "  Data Version: v${DATA_VERSION}"
    echo "  Output Dir:   ${OUTPUT_DIR}"
    echo "  ES Host:      ${ES_URL}"
    echo "  Extra Args:   ${PYTHON_ARGS:-none}"

    # Check for precomputed neural phonetics
    PRECOMPUTED_ARG=""
    if [ -f "$NEURAL_PHONETICS" ]; then
        echo "  Neural G2P:   ${NEURAL_PHONETICS} (found)"
        PRECOMPUTED_ARG="--precomputed-phonetics \"${NEURAL_PHONETICS}\""
    else
        echo "  Neural G2P:   not found (zh/ko/gan/wuu/yue/he will be skipped)"
        echo "                Run: es -precompute-phonetics ${DATA_VERSION}"
    fi
    echo
    echo "This job will:"
    echo "  1. Extract ALL toponyms from places index (with attestations)"
    echo "  2. Filter pre-romanized forms (lang-script mismatches)"
    echo "  3. Generate vocabulary (full Unicode ranges, native script)"
    echo "  4. Compute IPA + PanPhon embeddings for training namespace toponyms"
    echo "     Epitran: CPU parallel (Latin, Cyrillic, Greek, Arabic, Indic, etc.)"
    if [ -f "$NEURAL_PHONETICS" ]; then
        echo "     Neural: merged from precomputed (CharsiuG2P + Phonikud)"
    fi
    echo "  5. Index ALL toponyms to ES (panphon_embedding where available)"
    echo "  6. Generate name_romanized for cross-script text search"
    echo "  7. Refresh index and create snapshot"
    echo

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-rebuild-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/rebuild_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/rebuild_v${DATA_VERSION}_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

set -e

# Load Environment
source "$CONDA_SETUP_PATH"
conda activate whg

cd "$REPO_DIR"

# Setup scratch
SCRATCH_DIR="$SCRATCH_VAR"
mkdir -p "\$SCRATCH_DIR"
mkdir -p "$OUTPUT_DIR"

echo "=========================================="
echo "REBUILD TOPONYMS INDEX"
echo "=========================================="
echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Scratch: \$SCRATCH_DIR"
echo "Output:  $OUTPUT_DIR"
echo

# Run the rebuild script
python -u -m phonetics.extraction.rebuild_toponyms_index \
    --es-host "${ES_URL}" \
    --db-path "${DB_PATH}" \
    --output-dir "${OUTPUT_DIR}" \
    --scratch-dir "\$SCRATCH_DIR" \
    --training-namespaces gn wd tgn \
    --confirm \
    ${PRECOMPUTED_ARG} \
    $PYTHON_ARGS

echo
echo "=========================================="
echo "JOB COMPLETE"
echo "=========================================="
echo "Output directory: $OUTPUT_DIR"
echo "  - vocab/           Character, language, script vocabularies"
echo "  - coverage_stats.json  PanPhon coverage by script+language"
echo "  - toponyms.db  DuckDB checkpoint"
echo
echo "ES index: toponyms"
echo "  - Includes panphon_embedding for phonetic similarity queries"
echo "  - Includes name_romanized for cross-script text search"
echo
echo "Next steps:"
echo "  1. Review coverage_stats.json"
echo "  2. Generate training data: source es.sh -generate-training-data $DATA_VERSION"
echo
echo "Job Finished: \$(date)"
EOF
)

    # Strip cluster suffix from JOBID if present
    CLEAN_JOBID="${JOBID%;*}"

    echo "✓ Rebuild job submitted: $CLEAN_JOBID"
    echo "  Monitor: squeue -j $CLEAN_JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/rebuild_v${DATA_VERSION}_${CLEAN_JOBID}.*"
}

# ==============================================================================
# PARTIAL ES UPDATE (SPECIFIC LANGUAGES)
# ==============================================================================

do_partial_update_es() {
    # Usage: source es.sh -partial-update-es [VERSION] [--languages LANG1 LANG2 ...]
    #
    # Updates Elasticsearch documents for specific languages without full index rebuild.
    # Much faster than full rebuild when you only need to update a subset of documents.

    DATA_VERSION=${1:-7}
    shift 2>/dev/null || true

    # Parse --languages flag
    LANGUAGES=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --languages)
                shift
                # Collect all following args until next flag or end
                while [[ $# -gt 0 ]] && [[ ! "$1" =~ ^-- ]]; do
                    LANGUAGES="$LANGUAGES $1"
                    shift
                done
                ;;
            *)
                shift
                ;;
        esac
    done

    if [ -z "$LANGUAGES" ]; then
        echo "ERROR: --languages required"
        echo "Usage: es -partial-update-es VERSION --languages LANG1 [LANG2 ...]"
        echo "Example: es -partial-update-es 7 --languages ja"
        return 1
    fi

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

    ES_URL="http://${ES_NODE}:${ES_PORT}"
    DB_PATH="${IX1_BASE}/data/toponyms.db"

    # Verify DuckDB exists
    if [ ! -f "$DB_PATH" ]; then
        echo "ERROR: DuckDB not found at $DB_PATH"
        echo "The database must contain updated IPA data for the specified languages."
        return 1
    fi

    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/logs}"
    mkdir -p "$LOG_DIR"

    echo "=========================================="
    echo "PARTIAL ES UPDATE"
    echo "=========================================="
    echo "  Data Version: v${DATA_VERSION}"
    echo "  DuckDB:       ${DB_PATH}"
    echo "  ES Host:      ${ES_URL}"
    echo "  Languages:    ${LANGUAGES}"
    echo
    echo "This job will:"
    echo "  1. Query DuckDB for toponyms in specified languages"
    echo "  2. Bulk update ES documents with new IPA/PanPhon data"
    echo "  3. Refresh index"
    echo
    echo "Note: This does NOT rebuild the index - just updates existing documents."
    echo

    # Convert space-separated to comma-separated for Python
    LANG_ARGS=$(echo "$LANGUAGES" | tr ' ' '\n' | paste -sd ',' -)

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-partial-update-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/partial_update_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/partial_update_v${DATA_VERSION}_%j.err
#SBATCH --time=6:00:00
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G

set -e

# Load Environment
source "$CONDA_SETUP_PATH"
conda activate whg

cd "$REPO_DIR"

echo "=========================================="
echo "PARTIAL ES UPDATE"
echo "=========================================="
echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Languages: ${LANGUAGES}"
echo

# Run the partial update
python -u -m phonetics.extraction.rebuild_toponyms_index \
    --es-host "${ES_URL}" \
    --db-path "${DB_PATH}" \
    --toponyms-index toponyms \
    --languages ${LANG_ARGS} \
    --partial-update \
    --resume \
    --confirm

echo
echo "=========================================="
echo "PARTIAL UPDATE COMPLETE"
echo "=========================================="
echo "Check the output above for:"
echo "  - Updated: number of successfully updated documents"
echo "  - Not found: documents in DuckDB but not in ES"
echo
echo "Job Finished: \$(date)"
EOF
)

    CLEAN_JOBID="${JOBID%;*}"

    echo "✓ Partial update job submitted: $CLEAN_JOBID"
    echo "  Monitor: squeue -j $CLEAN_JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/partial_update_v${DATA_VERSION}_${CLEAN_JOBID}.*"
    echo
    echo "This typically completes in 10-30 minutes for a single language."
}

# ==============================================================================
# PRECOMPUTE NEURAL PHONETICS (GPU)
# ==============================================================================

do_precompute_phonetics() {
    # Usage: source es.sh -precompute-phonetics [VERSION]
    #
    # Runs CharsiuG2P (zh/ko/gan/wuu/yue) and Phonikud (he) on GPU.
    # Output: neural_phonetics.parquet in the data version directory.
    #
    # Must run AFTER -rebuild-toponyms (needs DuckDB with extracted toponyms).
    # Must run BEFORE the next -rebuild-toponyms --resume (which merges results).

    DATA_VERSION=${1:-6}

    OUTPUT_DIR="/ix1/ishi/models/phonetic/data/v${DATA_VERSION}"
    DB_PATH="${IX1_BASE}/data/toponyms.db"
    OUTPUT_FILE="${OUTPUT_DIR}/neural_phonetics.parquet"
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/logs}"

    if [ -z "$REPO_DIR" ]; then
        REPO_DIR="/ix1/ishi/elastic"
    fi

    mkdir -p "$LOG_DIR" "$OUTPUT_DIR"

    # Verify DuckDB exists
    if [ ! -f "$DB_PATH" ]; then
        echo "ERROR: DuckDB not found at $DB_PATH"
        echo "Run -rebuild-toponyms first to extract toponyms."
        return 1
    fi

    echo "=========================================="
    echo "PRECOMPUTE NEURAL PHONETICS (GPU)"
    echo "=========================================="
    echo "  Data Version: v${DATA_VERSION}"
    echo "  DuckDB:       ${DB_PATH}"
    echo "  Output:       ${OUTPUT_FILE}"
    echo
    echo "This job will:"
    echo "  1. Query DuckDB for neural-language toponyms (zh/ko/gan/wuu/yue/he)"
    echo "  2. Run CharsiuG2P (batched) on GPU for CJK/Korean"
    echo "  3. Run Phonikud on GPU for Hebrew"
    echo "  4. Compute PanPhon features and 192-dim embeddings"
    echo "  5. Save results to Parquet"
    echo
    echo "After completion, re-run:"
    echo "  es -rebuild-toponyms ${DATA_VERSION} --resume"
    echo "to merge neural phonetics into the JSONL and ES index."
    echo

    SCRATCH_VAR="/scratch/slurm-\${SLURM_JOB_ID}"

    JOBID=$(sbatch --parsable -M gpu <<EOF
#!/bin/bash
#SBATCH --job-name=whg-neural-g2p-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/neural_g2p_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/neural_g2p_v${DATA_VERSION}_%j.err
#SBATCH --time=12:00:00
#SBATCH --partition=a100
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G

echo "=========================================="
echo "PRECOMPUTE NEURAL PHONETICS (GPU)"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

set -e

source "$CONDA_SETUP_PATH"
conda activate whg

cd "${REPO_DIR}"

SCRATCH_DIR="$SCRATCH_VAR"
mkdir -p "\$SCRATCH_DIR"

python -u -m phonetics.extraction.precompute_neural_phonetics \
    --db-path "${DB_PATH}" \
    --output "${OUTPUT_FILE}" \
    --training-namespaces gn wd tgn \
    --batch-size 64 \
    --device cuda \
    --scratch-dir "\$SCRATCH_DIR"

echo
echo "=========================================="
echo "JOB COMPLETE"
echo "=========================================="
echo "Output: ${OUTPUT_FILE}"
echo
echo "Next: es -rebuild-toponyms ${DATA_VERSION} --resume"
echo "  (will merge neural phonetics into JSONL and ES index)"
echo
echo "Finished: \$(date)"
EOF
)

    CLEAN_JOBID=$(echo "$JOBID" | cut -d';' -f1)
    echo "✓ Neural G2P job submitted: $CLEAN_JOBID"
    echo "  Monitor: squeue -j $CLEAN_JOBID -M gpu"
    echo "  Logs: tail -f ${LOG_DIR}/neural_g2p_v${DATA_VERSION}_${CLEAN_JOBID}.*"
}

# ==============================================================================
# FORCE MERGE (purge deleted docs, reduce segment count)
# ==============================================================================

do_forcemerge() {
    # Usage: es -forcemerge [INDEX] [--max-segments N] [--no-iterative] [--step-factor N]
    #
    # Force-merges index segments to purge deleted documents and reduce
    # segment count. Run only when the index is no longer being written to.
    #
    # IMPORTANT: max_num_segments is a per-shard limit, but _cat/indices reports
    # the total across all shards. This function converts correctly.
    #
    # Iterative mode (default) halves the per-shard segment count each pass,
    # which is faster than jumping straight to 1 because each pass rewrites
    # less data. Progress is printed after each pass.
    #
    # WARNING: CPU/IO intensive. Do not run during active ingestion.

    INDEX="${1:-places}"
    shift 2>/dev/null || true

    MAX_SEGMENTS_PER_SHARD=1
    ITERATIVE=true
    STEP_FACTOR=2

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --max-segments)
                MAX_SEGMENTS_PER_SHARD="$2"
                shift 2
                ;;
            --no-iterative)
                ITERATIVE=false
                shift
                ;;
            --step-factor)
                STEP_FACTOR="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: No staging ES instance running"
        return 1
    fi
    source "$STAGING_INFO_FILE"

    if ! curl -s --connect-timeout 5 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to staging ES at http://${ES_NODE}:${ES_PORT}"
        return 1
    fi

    # ---- helper: run one forcemerge pass (per-shard target) and wait ----
    _run_merge_pass() {
        local target_per_shard="$1"
        echo -n "  Merging to ${target_per_shard} segment(s) per shard..."
        curl -s -X POST \
            "http://${ES_NODE}:${ES_PORT}/${INDEX}/_forcemerge?max_num_segments=${target_per_shard}&wait_for_completion=false" \
            -o /dev/null
        while true; do
            local count
            count=$(curl -s "http://${ES_NODE}:${ES_PORT}/_tasks?actions=indices:admin/forcemerge&detailed=false" \
                | python3 -c \
                    "import sys,json; t=json.load(sys.stdin).get('nodes',{}); print(sum(len(v.get('tasks',{})) for v in t.values()))" \
                    2>/dev/null || echo "0")
            [ "$count" -eq 0 ] && echo " done." && break
            echo -n "."
            sleep 10
        done
    }

    # ---- helper: print current index stats ----
    _print_stats() {
        curl -s "http://${ES_NODE}:${ES_PORT}/_cat/indices/${INDEX}?v&h=index,docs.count,docs.deleted,store.size,segments.count" \
            2>/dev/null
    }

    # ---- get shard count ----
    SHARD_COUNT=$(curl -s "http://${ES_NODE}:${ES_PORT}/_cat/shards/${INDEX}?h=shard&s=shard" \
        | grep -c . 2>/dev/null || echo "1")
    # Only primary shards matter for segment maths
    PRIMARY_SHARDS=$(curl -s "http://${ES_NODE}:${ES_PORT}/_cat/shards/${INDEX}?h=prirep" \
        | grep -c "^p" 2>/dev/null || echo "$SHARD_COUNT")
    [ "$PRIMARY_SHARDS" -eq 0 ] && PRIMARY_SHARDS=1

    echo "=========================================="
    echo "FORCE MERGE: ${INDEX}"
    echo "=========================================="
    echo
    echo "Before:"
    _print_stats
    echo

    DELETED=$(curl -s "http://${ES_NODE}:${ES_PORT}/_cat/indices/${INDEX}?h=docs.deleted" | tr -d ' \n')
    if [ "${DELETED:-0}" -eq 0 ] 2>/dev/null; then
        echo "No deleted documents - nothing to do."
        return 0
    fi

    TOTAL_SEGS=$(curl -s "http://${ES_NODE}:${ES_PORT}/_cat/indices/${INDEX}?h=segments.count" | tr -d ' \n')
    # Per-shard segment count (ceiling division)
    CURRENT_PER_SHARD=$(( (TOTAL_SEGS + PRIMARY_SHARDS - 1) / PRIMARY_SHARDS ))

    echo "Primary shards:    ${PRIMARY_SHARDS}"
    echo "Total segments:    ${TOTAL_SEGS}  (${CURRENT_PER_SHARD} per shard)"
    echo "Target:            ${MAX_SEGMENTS_PER_SHARD} per shard"
    echo "Deleted docs:      ${DELETED}"
    echo

    if [ "$CURRENT_PER_SHARD" -le "$MAX_SEGMENTS_PER_SHARD" ] 2>/dev/null; then
        echo "Already at or below target (${CURRENT_PER_SHARD} per shard). Running final merge to purge deleted docs."
        _run_merge_pass "$MAX_SEGMENTS_PER_SHARD"
    elif [ "$ITERATIVE" = "true" ]; then
        echo "Iterative mode: step factor ${STEP_FACTOR}, per-shard: ${CURRENT_PER_SHARD} -> ${MAX_SEGMENTS_PER_SHARD}"
        echo

        PASS=1
        PREV_PER_SHARD=0
        while [ "$CURRENT_PER_SHARD" -gt "$MAX_SEGMENTS_PER_SHARD" ]; do
            NEXT=$(( (CURRENT_PER_SHARD + STEP_FACTOR - 1) / STEP_FACTOR ))
            [ "$NEXT" -lt "$MAX_SEGMENTS_PER_SHARD" ] && NEXT=$MAX_SEGMENTS_PER_SHARD

            echo "Pass ${PASS}: ${CURRENT_PER_SHARD} -> ${NEXT} per shard  (total: ~$(( NEXT * PRIMARY_SHARDS )))"
            _run_merge_pass "$NEXT"
            _print_stats
            echo

            TOTAL_SEGS=$(curl -s "http://${ES_NODE}:${ES_PORT}/_cat/indices/${INDEX}?h=segments.count" | tr -d ' \n')
            CURRENT_PER_SHARD=$(( (TOTAL_SEGS + PRIMARY_SHARDS - 1) / PRIMARY_SHARDS ))

            # Guard: stop if not making progress
            if [ "$CURRENT_PER_SHARD" -ge "${PREV_PER_SHARD:-999}" ] && [ "$PASS" -gt 1 ]; then
                echo "  Segment count not decreasing further - jumping to final pass."
                break
            fi
            PREV_PER_SHARD=$CURRENT_PER_SHARD
            PASS=$(( PASS + 1 ))
        done

        # Final pass to reach exact target (also purges any remaining deleted docs)
        if [ "$CURRENT_PER_SHARD" -gt "$MAX_SEGMENTS_PER_SHARD" ]; then
            echo "Final pass: ${CURRENT_PER_SHARD} -> ${MAX_SEGMENTS_PER_SHARD} per shard"
            _run_merge_pass "$MAX_SEGMENTS_PER_SHARD"
        fi
    else
        echo "Direct merge: ${CURRENT_PER_SHARD} -> ${MAX_SEGMENTS_PER_SHARD} per shard"
        _run_merge_pass "$MAX_SEGMENTS_PER_SHARD"
    fi

    curl -s -X POST "http://${ES_NODE}:${ES_PORT}/${INDEX}/_refresh" -o /dev/null

    echo
    echo "After:"
    _print_stats
    echo
    echo "Done. Consider retaking the snapshot to capture the merged state."
}

# ==============================================================================
# GENERATE TRAINING DATA (Phase 2)
# ==============================================================================

do_generate_training_data() {
    # Usage: source es.sh -generate-training-data [VERSION] [--force|--resume|--skip-to-phase3|--resume-from-pass2]

    DATA_VERSION=${1:-6}
    shift 2>/dev/null || true

    # Parse flags
    FORCE_FLAG=""
    SKIP_PHASE3_FLAG=""
    RESUME_PASS2_FLAG=""
    PYTHON_ARGS=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --force)
                FORCE_FLAG="--force"
                echo "  Mode: FORCE (will regenerate all phases)"
                shift
                ;;
            --resume)
                echo "  Mode: RESUME (will skip completed phases)"
                shift
                ;;
            --skip-to-phase3)
                SKIP_PHASE3_FLAG="--skip-to-phase3"
                # es -generate-training-data 6 --skip-to-phase3
                echo "  Mode: SKIP TO PHASE 3 (assumes Phase 1 & 2 complete)"
                shift
                ;;
            --resume-from-pass2)
                RESUME_PASS2_FLAG="--resume-from-pass2"
                echo "  Mode: RESUME FROM PASS 2 (Phase 3 only, skips ES mining)"
                shift
                ;;
            *)
                PYTHON_ARGS="$PYTHON_ARGS $1"
                shift
                ;;
        esac
    done

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

    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/logs}"
    mkdir -p "$LOG_DIR"

    OUTPUT_DIR="/ix1/ishi/models/phonetic/data/v${DATA_VERSION}"
    DB_PATH="${IX1_BASE}/data/toponyms.db"

    # DuckDB is optional - we read training data from ES toponyms index
    DB_ARG=""
    if [ -f "$DB_PATH" ]; then
        DB_ARG="--db-path \"${DB_PATH}\""
        echo "  DuckDB found: ${DB_PATH} (optional, for fallback)"
    else
        echo "  DuckDB not found - will read from ES toponyms index"
    fi

    # Check ES toponyms index exists
    TOPONYM_COUNT=$(curl -s "http://${ES_NODE}:${ES_PORT}/toponyms/_count" | jq -r '.count // 0')
    if [ "$TOPONYM_COUNT" -eq 0 ]; then
        echo "ERROR: No documents in ES toponyms index"
        echo "Run -rebuild-toponyms first to populate the index."
        return 1
    fi
    echo "  ES toponyms: ${TOPONYM_COUNT} documents"

    # Show checkpoint status
    echo
    echo "Checkpoint status:"
    if [ -f "${OUTPUT_DIR}/pairs/positive_pairs.parquet" ]; then
        echo "  ✓ pairs/positive_pairs.parquet exists"
    else
        echo "  ○ pairs/positive_pairs.parquet (pending)"
    fi
    if [ -f "${OUTPUT_DIR}/triplets/phase1/train.parquet" ] && [ -f "${OUTPUT_DIR}/triplets/phase1/val.parquet" ]; then
        echo "  ✓ triplets/phase1/{train,val}.parquet exist"
    else
        echo "  ○ triplets/phase1/{train,val}.parquet (pending)"
    fi
    if [ -f "${OUTPUT_DIR}/training/phase2/train.parquet" ] && [ -f "${OUTPUT_DIR}/training/phase2/val.parquet" ]; then
        echo "  ✓ training/phase2/{train,val}.parquet exist"
    else
        echo "  ○ training/phase2/{train,val}.parquet (pending)"
    fi
    if [ -f "${OUTPUT_DIR}/triplets/phase3/train.parquet" ] && [ -f "${OUTPUT_DIR}/triplets/phase3/val.parquet" ]; then
        echo "  ✓ triplets/phase3/{train,val}.parquet exist"
    else
        echo "  ○ triplets/phase3/{train,val}.parquet (pending)"
    fi

    SCRATCH_VAR="/scratch/slurm-\${SLURM_JOB_ID}"

    echo
    echo "=========================================="
    echo "GENERATE TRAINING DATA"
    echo "=========================================="
    echo "  Data Version: v${DATA_VERSION}"
    echo "  Output Dir:   ${OUTPUT_DIR}"
    echo "  ES Host:      http://${ES_NODE}:${ES_PORT}"
    if [ -n "$FORCE_FLAG" ]; then
        echo "  Mode:         FORCE (regenerate all)"
    elif [ -n "$SKIP_PHASE3_FLAG" ]; then
        echo "  Mode:         SKIP TO PHASE 3"
    elif [ -n "$RESUME_PASS2_FLAG" ]; then
        echo "  Mode:         RESUME FROM PASS 2 (Phase 3 only, skips ES mining)"
    else
        echo "  Mode:         RESUME (skip completed phases)"
    fi
    echo

    JOBID=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-traindata-v${DATA_VERSION}
#SBATCH --output=${LOG_DIR}/traindata_v${DATA_VERSION}_%j.out
#SBATCH --error=${LOG_DIR}/traindata_v${DATA_VERSION}_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=300G

set -e

# Load Environment
source "$CONDA_SETUP_PATH"
conda activate whg

cd "$REPO_DIR"

# Setup scratch
SCRATCH_DIR="$SCRATCH_VAR"
mkdir -p "\$SCRATCH_DIR"

echo "=========================================="
echo "GENERATE TRAINING DATA"
echo "=========================================="
echo "Job Started: \$(date)"
echo "Node: \$(hostname)"
echo "Scratch: \$SCRATCH_DIR"
echo "Output:  $OUTPUT_DIR"
echo

# Run the training data generation script
python -m phonetics.extraction.generate_training_data \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    ${DB_ARG} \
    --output-dir "${OUTPUT_DIR}" \
    --scratch-dir "\$SCRATCH_DIR" \
    --training-namespaces gn wd tgn \
    $FORCE_FLAG \
    $SKIP_PHASE3_FLAG \
    $RESUME_PASS2_FLAG \
    $PYTHON_ARGS

echo
echo "=========================================="
echo "JOB COMPLETE"
echo "=========================================="
echo "Output directory: $OUTPUT_DIR"
echo "  - pairs/          Positive pairs Parquet"
echo "  - triplets/       Phase 1 & 3 triplets"
echo "  - training/       Phase 2 samples"
echo "  - training_stats.json  Sample distribution"
echo
echo "Next steps:"
echo "  1. Review training_stats.json for balance"
echo "  2. Train model: source es.sh -train-model $DATA_VERSION"
echo
echo "Job Finished: \$(date)"
EOF
)

    echo "✓ Training data job submitted: $JOBID"
    echo "  Monitor: squeue -j $JOBID"
    echo "  Logs: tail -f ${LOG_DIR}/traindata_v${DATA_VERSION}_${JOBID}.out"
}

# ==============================================================================
# TRAIN MODEL (Phase 3)
# ==============================================================================

do_train_model() {
    # Usage: es -train-model VERSION [START_PHASE [END_PHASE]] [--resume] [--resume-from CHECKPOINT] [--partition PARTITION]
    # Positional args (VERSION, START_PHASE, END_PHASE) must come before any flags.
    # Examples:
    #   es -train-model 7                          # all phases (1-3), a100
    #   es -train-model 7 --partition l40s         # all phases (1-3), l40s
    #   es -train-model 7 1 --partition l40s       # phase 1 only, l40s
    #   es -train-model 7 1 3 --partition l40s     # phases 1-3, l40s
    #   es -train-model 7 2 --resume               # phase 2 only, auto-resume
    #   es -train-model 7 2 3 --resume             # phases 2-3, auto-resume

    # Parse positional args first (stop at first flag)
    DATA_VERSION="${1:-6}"
    shift || true

    START_PHASE=1
    END_PHASE=""
    START_PHASE_EXPLICIT=false
    if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
        START_PHASE="$1"
        START_PHASE_EXPLICIT=true
        shift
        if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
            END_PHASE="$1"
            shift
        fi
    fi

    # Now parse flags
    GPU_PARTITION="${GPU_PARTITION:-a100}"
    RESUME_FROM=""
    AUTO_RESUME=false

    while [ $# -gt 0 ]; do
        case "$1" in
            --resume)
                AUTO_RESUME=true
                shift
                ;;
            --resume-from)
                RESUME_FROM="$2"
                shift 2
                ;;
            --partition)
                GPU_PARTITION="$2"
                shift 2
                ;;
            *)
                echo "WARNING: Unknown argument ignored: $1"
                shift
                ;;
        esac
    done

    # END_PHASE default:
    #   es -train-model 7            -> phases 1-3 (run everything)
    #   es -train-model 7 2          -> phase 2 only
    #   es -train-model 7 1 3        -> phases 1-3 (explicit)
    if [ -z "$END_PHASE" ]; then
        if [ "$START_PHASE_EXPLICIT" = "true" ]; then
            END_PHASE=$START_PHASE   # single phase specified
        else
            END_PHASE=3              # no phase specified: run all
        fi
    fi

    DATA_DIR="/ix1/ishi/models/phonetic/data/v${DATA_VERSION}"
    OUTPUT_DIR="/ix1/ishi/models/phonetic/checkpoints/v${DATA_VERSION}"
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/staging-logs}"

    if [ -z "$REPO_DIR" ]; then
        REPO_DIR="/ix1/ishi/elastic"
    fi

    TRAIN_LOG_DIR="${LOG_DIR}/training_v${DATA_VERSION}"
    mkdir -p "$TRAIN_LOG_DIR" "$OUTPUT_DIR"

    echo "  Data dir: ${DATA_DIR}"
    echo "  Output dir: ${OUTPUT_DIR}"
    echo "  Log dir: ${TRAIN_LOG_DIR}"
    echo "  Repo dir: ${REPO_DIR}"

    # If --resume flag is set and no explicit --resume-from, enable auto-detection
    if [ "$AUTO_RESUME" = true ] && [ -z "$RESUME_FROM" ]; then
        echo "  Auto-resume: ENABLED (will detect latest checkpoint)"
    fi

    # Verify training data exists - check for actual content, not just parent dirs
    # Note: use if-then rather than [ ] && to avoid set -e aborting on false tests
    HAS_PHASE1="no"
    HAS_TRAINING="no"
    if [ -f "${DATA_DIR}/triplets/phase1/train.parquet" ]; then HAS_PHASE1="yes"; fi
    if [ -d "${DATA_DIR}/training/split=train" ]; then HAS_TRAINING="yes"; fi
    if [ -d "${DATA_DIR}/training/phase2" ]; then HAS_TRAINING="yes"; fi
    if [ "$HAS_PHASE1" = "no" ] && [ "$HAS_TRAINING" = "no" ]; then
        echo "ERROR: Training data not found at ${DATA_DIR}"
        echo "  Expected: ${DATA_DIR}/triplets/phase1/train.parquet"
        echo "  Or:       ${DATA_DIR}/training/split=train/"
        echo "  Run: es -generate-training-data ${DATA_VERSION} first"
        return 1
    fi

    # Pre-flight check
    echo ""
    echo "Pre-flight directory check:"
    echo "  ${DATA_DIR}/triplets/phase1:"
    if [ -f "${DATA_DIR}/triplets/phase1/train.parquet" ] && [ -f "${DATA_DIR}/triplets/phase1/val.parquet" ]; then
        ls -lh "${DATA_DIR}/triplets/phase1/"*.parquet 2>/dev/null
    else
        echo "    [not found - run: es -generate-training-data ${DATA_VERSION}]"
    fi

    echo "  ${DATA_DIR}/triplets/phase3:"
    if [ -f "${DATA_DIR}/triplets/phase3/train.parquet" ] && [ -f "${DATA_DIR}/triplets/phase3/val.parquet" ]; then
        ls -lh "${DATA_DIR}/triplets/phase3/"*.parquet 2>/dev/null
    else
        echo "    [not found - will be skipped if running phase 3]"
    fi

    echo "  ${DATA_DIR}/training:"
    ls -la "${DATA_DIR}/training" 2>/dev/null | head -10 || echo "    [not found]"
    echo "  ${DATA_DIR}/vocab:"
    ls -la "${DATA_DIR}/vocab" 2>/dev/null || echo "    [not found]"
    echo ""

    echo "=========================================="
    echo "SUBMITTING TRAINING PIPELINE (v${DATA_VERSION})"
    echo "Config: 1x GPU (${GPU_PARTITION}), 300GB RAM, 48H Limit"
    echo "Phases: ${START_PHASE} to ${END_PHASE}"
    echo "=========================================="

    # Phase 1: Train Teacher
    PHASE1_DEP=""
    if [ "$START_PHASE" -le 1 ] && [ "$END_PHASE" -ge 1 ]; then
        # Auto-detect best checkpoint if AUTO_RESUME is enabled or if RESUME_FROM is already set
        if [ -z "${RESUME_FROM}" ] && [ "$AUTO_RESUME" = true ]; then
            # Prefer phase1_best.pt if it exists
            if [ -f "${OUTPUT_DIR}/phase1_best.pt" ]; then
                RESUME_FROM="${OUTPUT_DIR}/phase1_best.pt"
                echo "📍 Auto-detected Phase 1 best checkpoint: $(basename $RESUME_FROM)"
            else
                # Fallback to latest epoch checkpoint
                LATEST_P1=$(find "${OUTPUT_DIR}" -name "phase1_epoch*.pt" 2>/dev/null | sort -V | tail -n1)
                if [ -n "$LATEST_P1" ]; then
                    RESUME_FROM="$LATEST_P1"
                    echo "📍 Auto-detected Phase 1 checkpoint: $(basename $RESUME_FROM)"
                fi
            fi
        fi

        # Check if Phase 1 training is actually complete
        PHASE1_COMPLETE=false
        if [ -f "${OUTPUT_DIR}/phase1_metrics.json" ]; then
            COMPLETED_EPOCHS=$(python3 -c "import json; print(len(json.load(open('${OUTPUT_DIR}/phase1_metrics.json'))['epochs']))" 2>/dev/null || echo "0")
            if [ "$COMPLETED_EPOCHS" -ge 50 ]; then
                PHASE1_COMPLETE=true
                echo "✓ Phase 1 training complete ($COMPLETED_EPOCHS/50 epochs), skipping"
            elif [ -n "${RESUME_FROM}" ]; then
                NEXT_EPOCH=$((COMPLETED_EPOCHS + 1))
                echo "⏸ Phase 1 resuming from epoch $NEXT_EPOCH/50"
            fi
        fi

        if [ "$PHASE1_COMPLETE" = false ]; then
            PHASE1_JOB=$(sbatch --parsable -M gpu <<EOF
#!/bin/bash
#SBATCH --job-name=whg-train-p1-v${DATA_VERSION}
#SBATCH --output=${TRAIN_LOG_DIR}/phase1_%j.out
#SBATCH --error=${TRAIN_LOG_DIR}/phase1_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

echo "===========================================" >&2
echo "Phase 1 Training Job Started" >&2
echo "Job ID: \$SLURM_JOB_ID" >&2
echo "Node: \$(hostname)" >&2
echo "Time: \$(date)" >&2
echo "===========================================" >&2

echo "Job started on \$(hostname) at \$(date)"
echo "SLURM_JOB_ID: \$SLURM_JOB_ID"

if [ ! -d "${DATA_DIR}" ]; then
    echo "ERROR: Data directory not found: ${DATA_DIR}" >&2
    exit 1
fi

if [ ! -d "${REPO_DIR}" ]; then
    echo "ERROR: Repo directory not found: ${REPO_DIR}" >&2
    exit 1
fi

if [ ! -f "${DATA_DIR}/triplets/phase1/train.parquet" ]; then
    echo "ERROR: Phase 1 train.parquet not found" >&2
    exit 1
fi

if [ ! -d "${DATA_DIR}/training" ]; then
    echo "ERROR: Training data directory not found at ${DATA_DIR}/training" >&2
    exit 1
fi

set -e

source "$CONDA_SETUP_PATH"
conda activate whg

cd "${REPO_DIR}"

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH_ROOT/triplets/phase1"
mkdir -p "\$SCRATCH_ROOT/vocab"
mkdir -p "\$SCRATCH_ROOT/training"

echo "Staging data to \$SCRATCH_ROOT..."
rsync -av "${DATA_DIR}/triplets/phase1/" "\$SCRATCH_ROOT/triplets/phase1/"
rsync -av "${DATA_DIR}/vocab/" "\$SCRATCH_ROOT/vocab/"
rsync -av "${DATA_DIR}/training/" "\$SCRATCH_ROOT/training/"

echo "Starting Phase 1 (Teacher)..."
python -u -m phonetics.training.train \
    --phase 1 \
    --data-dir "\$SCRATCH_ROOT" \
    --output-dir "${OUTPUT_DIR}" \
    --epochs 50\$([ -n "${RESUME_FROM}" ] && echo " --resume-from ${RESUME_FROM}" || echo "")
EOF
)
            PHASE1_JOB=$(echo "$PHASE1_JOB" | cut -d';' -f1)
            echo "✓ Phase 1 submitted: $PHASE1_JOB"
            PHASE1_DEP="--dependency=afterok:${PHASE1_JOB}"
        fi  # Close PHASE1_COMPLETE check
    else
        echo "✓ Phase 1 skipped"
    fi

    # Phase 2: Align Student to Teacher
    PHASE2_DEP=""
    if [ "$START_PHASE" -le 2 ] && [ "$END_PHASE" -ge 2 ]; then
        if [ ! -f "${OUTPUT_DIR}/phase1_best.pt" ] && [ -z "$PHASE1_JOB" ]; then
            echo "ERROR: Phase 1 checkpoint not found and no Phase 1 job submitted"
            return 1
        fi

        # Auto-detect best Phase 2 checkpoint if AUTO_RESUME is enabled
        if [ -z "${RESUME_FROM}" ] && [ "$AUTO_RESUME" = true ]; then
            # Prefer phase2_best.pt if it exists
            if [ -f "${OUTPUT_DIR}/phase2_best.pt" ]; then
                RESUME_FROM="${OUTPUT_DIR}/phase2_best.pt"
                echo "📍 Auto-detected Phase 2 best checkpoint: $(basename $RESUME_FROM)"
            else
                # Fallback to latest epoch checkpoint
                LATEST_P2=$(find "${OUTPUT_DIR}" -name "phase2_epoch_*.pt" 2>/dev/null | sort -V | tail -n1)
                if [ -n "$LATEST_P2" ]; then
                    RESUME_FROM="$LATEST_P2"
                    echo "📍 Auto-detected Phase 2 checkpoint: $(basename $RESUME_FROM)"
                fi
            fi
        fi

        # Check if Phase 2 training is actually complete
        PHASE2_COMPLETE=false
        if [ -f "${OUTPUT_DIR}/phase2_metrics.json" ]; then
            COMPLETED_EPOCHS=$(python3 -c "import json; print(len(json.load(open('${OUTPUT_DIR}/phase2_metrics.json'))['epochs']))" 2>/dev/null || echo "0")
            if [ "$COMPLETED_EPOCHS" -ge 50 ]; then
                PHASE2_COMPLETE=true
                echo "✓ Phase 2 training complete ($COMPLETED_EPOCHS/50 epochs), skipping"
            elif [ -n "${RESUME_FROM}" ]; then
                NEXT_EPOCH=$((COMPLETED_EPOCHS + 1))
                echo "⏸ Phase 2 resuming from epoch $NEXT_EPOCH/50"
            fi
        fi

        if [ "$PHASE2_COMPLETE" = false ]; then
            PHASE2_JOB=$(sbatch --parsable -M gpu $PHASE1_DEP <<EOF
#!/bin/bash
#SBATCH --job-name=whg-train-p2-v${DATA_VERSION}
#SBATCH --output=${TRAIN_LOG_DIR}/phase2_%j.out
#SBATCH --error=${TRAIN_LOG_DIR}/phase2_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

echo "Job started on \$(hostname) at \$(date)"

set -e

source "$CONDA_SETUP_PATH"
conda activate whg

cd "${REPO_DIR}"

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH_ROOT/training"
mkdir -p "\$SCRATCH_ROOT/vocab"

rsync -a "${DATA_DIR}/training/" "\$SCRATCH_ROOT/training/"
rsync -a "${DATA_DIR}/vocab/" "\$SCRATCH_ROOT/vocab/"

echo "Starting Phase 2 (Student Alignment)..."
python -u -m phonetics.training.train \
    --phase 2 \
    --data-dir "\$SCRATCH_ROOT" \
    --output-dir "${OUTPUT_DIR}" \
    --teacher-checkpoint "${OUTPUT_DIR}/phase1_best.pt" \
    --epochs 50\$([ -n "${RESUME_FROM}" ] && echo " --resume-from ${RESUME_FROM}" || echo "")
EOF
)
            PHASE2_JOB=$(echo "$PHASE2_JOB" | cut -d';' -f1)
            echo "✓ Phase 2 submitted: $PHASE2_JOB"
            PHASE2_DEP="--dependency=afterok:${PHASE2_JOB}"
        fi  # Close PHASE2_COMPLETE check
    else
        echo "✓ Phase 2 skipped"
    fi

    # Phase 3: Fine-tune with hard negatives
    if [ "$START_PHASE" -le 3 ] && [ "$END_PHASE" -ge 3 ]; then
        if [ ! -f "${OUTPUT_DIR}/phase2_best.pt" ] && [ -z "$PHASE2_JOB" ]; then
            echo "ERROR: Phase 2 checkpoint not found and no Phase 2 job submitted"
            return 1
        fi

        # Auto-detect best Phase 3 checkpoint if AUTO_RESUME is enabled
        if [ -z "${RESUME_FROM}" ] && [ "$AUTO_RESUME" = true ]; then
            # Prefer phase3_best.pt if it exists
            if [ -f "${OUTPUT_DIR}/phase3_best.pt" ]; then
                RESUME_FROM="${OUTPUT_DIR}/phase3_best.pt"
                echo "📍 Auto-detected Phase 3 best checkpoint: $(basename $RESUME_FROM)"
            else
                # Fallback to latest epoch checkpoint
                LATEST_P3=$(find "${OUTPUT_DIR}" -name "phase3_epoch_*.pt" 2>/dev/null | sort -V | tail -n1)
                if [ -n "$LATEST_P3" ]; then
                    RESUME_FROM="$LATEST_P3"
                    echo "📍 Auto-detected Phase 3 checkpoint: $(basename $RESUME_FROM)"
                fi
            fi
        fi

        # Check if Phase 3 training is actually complete
        PHASE3_COMPLETE=false
        if [ -f "${OUTPUT_DIR}/phase3_metrics.json" ]; then
            COMPLETED_EPOCHS=$(python3 -c "import json; print(len(json.load(open('${OUTPUT_DIR}/phase3_metrics.json'))['epochs']))" 2>/dev/null || echo "0")
            if [ "$COMPLETED_EPOCHS" -ge 30 ]; then
                PHASE3_COMPLETE=true
                echo "✓ Phase 3 training complete ($COMPLETED_EPOCHS/30 epochs), skipping"
            elif [ -n "${RESUME_FROM}" ]; then
                NEXT_EPOCH=$((COMPLETED_EPOCHS + 1))
                echo "⏸ Phase 3 resuming from epoch $NEXT_EPOCH/30"
            fi
        fi

        if [ "$PHASE3_COMPLETE" = false ]; then
            PHASE3_JOB=$(sbatch --parsable -M gpu $PHASE2_DEP <<EOF
#!/bin/bash
#SBATCH --job-name=whg-train-p3-v${DATA_VERSION}
#SBATCH --output=${TRAIN_LOG_DIR}/phase3_%j.out
#SBATCH --error=${TRAIN_LOG_DIR}/phase3_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G

echo "Job started on \$(hostname) at \$(date)"

set -e

source "$CONDA_SETUP_PATH"
conda activate whg

cd "${REPO_DIR}"

SCRATCH_ROOT="/scratch/slurm-\${SLURM_JOB_ID}"
mkdir -p "\$SCRATCH_ROOT/triplets/phase3"
mkdir -p "\$SCRATCH_ROOT/vocab"

rsync -a "${DATA_DIR}/triplets/phase3/" "\$SCRATCH_ROOT/triplets/phase3/"
rsync -a "${DATA_DIR}/vocab/" "\$SCRATCH_ROOT/vocab/"

echo "Starting Phase 3 (Fine Tuning)..."
python -u -m phonetics.training.train \
    --phase 3 \
    --data-dir "\$SCRATCH_ROOT" \
    --output-dir "${OUTPUT_DIR}" \
    --student-checkpoint "${OUTPUT_DIR}/phase2_best.pt" \
    --epochs 30\$([ -n "${RESUME_FROM}" ] && echo " --resume-from ${RESUME_FROM}" || echo "")
EOF
)
            PHASE3_JOB=$(echo "$PHASE3_JOB" | cut -d';' -f1)
            echo "✓ Phase 3 submitted: $PHASE3_JOB"
        fi  # Close PHASE3_COMPLETE check
    else
        echo "✓ Phase 3 skipped"
    fi

    echo
    echo "Pipeline queued. Monitor: squeue -u stg135"
    echo "tail -f ${TRAIN_LOG_DIR}/*_.*"
}

# ==============================================================================
# TRAIN AND UPDATE (Full Pipeline)
# ==============================================================================

do_train_and_update() {
    DATA_VERSION=${1:-6}

    echo "=========================================="
    echo "FULL PIPELINE: TRAIN AND UPDATE (v${DATA_VERSION})"
    echo "=========================================="
    echo
    echo "This will:"
    echo "  1. Train model phases 1-3"
    echo "  2. Generate embeddings for all toponyms"
    echo "  3. Update ES toponyms index"
    echo

    do_train_model "$DATA_VERSION"

    echo
    echo "Note: After training completes, run:"
    echo "  source es.sh -update-embeddings $DATA_VERSION"
    echo "to generate and index the embeddings."
}

# ==============================================================================
# UPDATE EMBEDDINGS (Compute + Index)
# ==============================================================================

do_update_embeddings() {
    DATA_VERSION=${1:-6}
    shift || true

    # Default to l40s for embedding computation unless explicitly overridden
    GPU_PARTITION="${GPU_PARTITION:-l40s}"
    AFTER_JOB=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --after)
                AFTER_JOB="$2"
                shift 2
                ;;
            --partition)
                GPU_PARTITION="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    DATA_DIR="/ix1/ishi/models/phonetic/data/v${DATA_VERSION}"
    CHECKPOINT_DIR="/ix1/ishi/models/phonetic/checkpoints/v${DATA_VERSION}"
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/staging-logs}"

    if [ -z "$REPO_DIR" ]; then
        REPO_DIR="/ix1/ishi/elastic"
    fi

    EMBEDDINGS_LOG_DIR="${LOG_DIR}/embeddings_v${DATA_VERSION}"
    mkdir -p "$EMBEDDINGS_LOG_DIR"

    # Verify required files exist
    if [ ! -f "${CHECKPOINT_DIR}/phase3_best.pt" ]; then
        echo "ERROR: Phase 3 checkpoint not found at ${CHECKPOINT_DIR}/phase3_best.pt"
        echo "Train the model first: es -train-model ${DATA_VERSION}"
        return 1
    fi

    if [ ! -f "${IX1_BASE}/data/toponyms.db" ]; then
        echo "ERROR: DuckDB database not found at ${IX1_BASE}/data/toponyms.db"
        echo "Rebuild toponyms first: es -rebuild-toponyms ${DATA_VERSION}"
        return 1
    fi

    # Check if staging ES is running (needed for index step)
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: No staging ES instance running"
        echo "Start one first with: source es.sh -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    echo "=========================================="
    echo "EMBEDDING PIPELINE (v${DATA_VERSION})"
    echo "=========================================="
    echo "  Checkpoint: ${CHECKPOINT_DIR}/phase3_best.pt"
    echo "  DuckDB:     ${IX1_BASE}/data/toponyms.db"
    echo "  ES Host:    http://${ES_NODE}:${ES_PORT}"
    echo "  Logs:       ${EMBEDDINGS_LOG_DIR}"
    echo "  GPU Part:   ${GPU_PARTITION}"
    if [ -n "$AFTER_JOB" ]; then
        echo "  Depends on: ${AFTER_JOB} (afterok)"
    fi
    echo

    EMBEDDINGS_FILE="${DATA_DIR}/embeddings_v${DATA_VERSION}.parquet"

    # Dependency flag for sbatch
    DEP_FLAG=""
    if [ -n "$AFTER_JOB" ]; then
        DEP_FLAG="--dependency=afterok:${AFTER_JOB}"
    fi

    # Step 1: Compute embeddings (GPU)
    echo "Step 1: Submitting compute job (GPU)..."
    echo "  Note: compute job will submit the SMP index job itself on completion"
    echo "  (cross-cluster Slurm dependencies are not supported on Pitt CRC)"

    COMPUTE_JOB=$(sbatch --parsable -M gpu ${DEP_FLAG} <<EOF
#!/bin/bash
#SBATCH --job-name=whg-embed-compute-v${DATA_VERSION}
#SBATCH --output=${EMBEDDINGS_LOG_DIR}/compute_%j.out
#SBATCH --error=${EMBEDDINGS_LOG_DIR}/compute_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --gres=gpu:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=100G

echo "=========================================="
echo "COMPUTE EMBEDDINGS (v${DATA_VERSION})"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

set -e

source "${CONDA_SETUP_PATH}"
conda activate whg

cd "${REPO_DIR}"

echo "Computing embeddings from DuckDB (ALL toponyms)..."
python -u -m phonetics.inference.update_es compute \
    --input-file "${IX1_BASE}/data/toponyms.db" \
    --output-file "${EMBEDDINGS_FILE}" \
    --checkpoint "${CHECKPOINT_DIR}/phase3_best.pt" \
    --vocab-dir "${DATA_DIR}/vocab" \
    --embedding-version ${DATA_VERSION} \
    --batch-size 2000 \
    --device cuda

echo
echo "Embeddings saved to: ${EMBEDDINGS_FILE}"
echo "Finished: \$(date)"
echo

# Cross-cluster handoff: submit the SMP index job from inside the GPU job.
# Slurm --dependency cannot span clusters on Pitt CRC, so we submit here
# after confirming the compute step succeeded (set -e ensures we only reach
# this point if python exited 0).
echo "Submitting SMP index job..."
INDEX_JOB=\$(sbatch --parsable -M smp <<INNER
#!/bin/bash
#SBATCH --job-name=whg-embed-index-v${DATA_VERSION}
#SBATCH --output=${EMBEDDINGS_LOG_DIR}/index_%j.out
#SBATCH --error=${EMBEDDINGS_LOG_DIR}/index_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G

set -e
source "${CONDA_SETUP_PATH}"
conda activate whg
cd "${REPO_DIR}"

source "${STAGING_INFO_FILE}"

echo "Indexing embeddings to ES at http://\\\${ES_NODE}:\\\${ES_PORT}..."
python -u -m phonetics.inference.update_es index \
    --input-file "${EMBEDDINGS_FILE}" \
    --db-path "${IX1_BASE}/data/toponyms.db" \
    --es-host "http://\\\${ES_NODE}:\\\${ES_PORT}" \
    --index toponyms \
    --embedding-version ${DATA_VERSION}

echo "Indexing complete: \\\$(date)"
INNER
)
echo "✓ Index job submitted to SMP: \${INDEX_JOB}"
echo "  Monitor: squeue -j \${INDEX_JOB}"
echo "  Logs: tail -f ${EMBEDDINGS_LOG_DIR}/index_*.out"
EOF
)

    COMPUTE_JOB=$(echo "$COMPUTE_JOB" | cut -d';' -f1)
    echo "✓ Compute job submitted: ${COMPUTE_JOB}"
    echo "  Monitor: squeue -j ${COMPUTE_JOB} -M gpu"
    echo "  Logs: tail -f ${EMBEDDINGS_LOG_DIR}/compute_*.out"
    echo "  (Index job will be submitted automatically on completion)"
}

do_update_embeddings_index() {
    DATA_VERSION=${1:-6}
    shift || true

    DATA_DIR="/ix1/ishi/models/phonetic/data/v${DATA_VERSION}"
    LOG_DIR="${STAGING_SLURM_LOGS:-/ix1/ishi/es/staging-logs}"

    if [ -z "$REPO_DIR" ]; then
        REPO_DIR="/ix1/ishi/elastic"
    fi

    EMBEDDINGS_LOG_DIR="${LOG_DIR}/embeddings_v${DATA_VERSION}"
    mkdir -p "$EMBEDDINGS_LOG_DIR"

    EMBEDDINGS_FILE="${DATA_DIR}/embeddings_v${DATA_VERSION}.parquet"

    # Only check file existence when not depending on a prior job
    # (when --after is set the file doesn't exist yet at submission time)
    if [ -z "$AFTER_JOB" ] && [ ! -f "${EMBEDDINGS_FILE}" ]; then
        echo "ERROR: Embeddings file not found at ${EMBEDDINGS_FILE}"
        echo "Run compute step first: es -update-embeddings ${DATA_VERSION}"
        return 1
    fi

    # Check if staging ES is running
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: No staging ES instance running"
        echo "Start one first with: source es.sh -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    echo "=========================================="
    echo "INDEX EMBEDDINGS (v${DATA_VERSION})"
    echo "=========================================="
    echo "  Embeddings: ${EMBEDDINGS_FILE}"
    echo "  DuckDB:     ${IX1_BASE}/data/toponyms.db"
    echo "  ES Host:    http://${ES_NODE}:${ES_PORT}"
    echo "  Logs:       ${EMBEDDINGS_LOG_DIR}"
    echo

    INDEX_JOB=$(sbatch --parsable <<EOF
#!/bin/bash
#SBATCH --job-name=whg-embed-index-v${DATA_VERSION}
#SBATCH --output=${EMBEDDINGS_LOG_DIR}/index_%j.out
#SBATCH --error=${EMBEDDINGS_LOG_DIR}/index_%j.err
#SBATCH --time=48:00:00
#SBATCH --partition=smp
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=100G

echo "=========================================="
echo "INDEX EMBEDDINGS (v${DATA_VERSION})"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

set -e

source "$CONDA_SETUP_PATH"
conda activate whg

cd "${REPO_DIR}"

echo "Rebuilding ES index from DuckDB + embeddings..."
python -u -m phonetics.inference.update_es index \
    --duckdb-file "${IX1_BASE}/data/toponyms.db" \
    --embeddings-file "${EMBEDDINGS_FILE}" \
    --schema-file "${REPO_DIR}/schemas/toponyms.json" \
    --es-host "http://${ES_NODE}:${ES_PORT}" \
    --index toponyms \
    --embedding-version ${DATA_VERSION} \
    --batch-size 2000

echo
echo "Index rebuilt successfully."
echo "Snapshot created: toponyms_v${DATA_VERSION}"
echo "Finished: \$(date)"
EOF
)

    INDEX_JOB=$(echo "$INDEX_JOB" | cut -d';' -f1)
    echo "✓ Index job submitted: ${INDEX_JOB}"
    echo "  Monitor: squeue -j ${INDEX_JOB}"
    echo "  Logs: tail -f ${EMBEDDINGS_LOG_DIR}/index_*.out"
}

# =============================================================================
# MAIN
# =============================================================================

case "$1" in
    # --- Security Setup (one-time) ---
    -setup-security)
        do_setup_security
        ;;

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
        shift
        do_ingest "$@"
        ;;

    # --- Force Merge (purge deleted docs) ---
    -forcemerge)
        shift
        do_forcemerge "$@"
        ;;

    # --- Rebuild Toponyms Index ---
    -rebuild-toponyms)
        shift
        do_rebuild_toponyms "$@"
        ;;

    # --- Partial ES Update (Specific Languages) ---
    -partial-update-es)
        shift
        do_partial_update_es "$@"
        ;;

    # --- Precompute Neural Phonetics (GPU) ---
    -precompute-phonetics)
        shift
        do_precompute_phonetics "$@"
        ;;

    # --- Generate Training Data ---
    -generate-training-data)
        shift
        do_generate_training_data "$@"
        ;;

    # --- Training Pipeline ---
    -train-model)
        shift
        do_train_model "$@"
        ;;

    # --- Full Pipeline: Train + Embeddings + Index ---
    -train-and-update)
        shift
        do_train_and_update "$@"
        ;;

    # --- Inference / Embedding Pipeline ---
    -update-embeddings)
        shift
        do_update_embeddings "$@"
        ;;

    # --- Index Embeddings (after compute step) ---
    -update-embeddings-index)
        shift
        do_update_embeddings_index "$@"
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
        shift
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
        echo "  -setup-security     One-time: set ES passwords + enable Kibana HTTPS auth"
        echo "                      Run after certbot has issued TLS certificates"
        echo
        echo "PIPELINE (Symphonym v6):"
        echo "  -rebuild-toponyms   [VER] Extract toponyms + Epitran IPA + index"
        echo "  -partial-update-es   [VER] Update specific languages in ES (no full rebuild)"
        echo "  -precompute-phonetics [VER] Neural G2P on GPU (CharsiuG2P + Phonikud)"
        echo "  -generate-training-data [VER] Generate training sets"
        echo "  -train-model        [VER] Train Teacher/Student models"
        echo "  -update-embeddings  [VER] Compute new embeddings and index"
        echo
        echo "HEALTH CHECKS:"
        echo "  -health             Production cluster health and stats"
        echo "  -staging-health     Staging cluster health and stats"
        echo "  -forcemerge [INDEX] Purge deleted docs, merge segments (default index: places)"
        echo "              --max-segments N   Target segments per shard (default: 1)"
        echo "              --step-factor N    Divisor per iterative pass (default: 2 = halve)"
        echo "              --no-iterative     Skip iterative mode, merge direct to target"
        echo "              Iterative mode is on by default: halves segment count each pass"
        echo "              until target is reached (much faster for large indices)."
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
        echo "  -rebuild-toponyms VERSION [OPTIONS]"
        echo "      Extracts toponyms from places, computes IPA + PanPhon embeddings,"
        echo "      and indexes to ES with panphon_embedding for phonetic similarity."
        echo
        echo "      IPA backends:"
        echo "        Epitran (CPU parallel): Latin, Cyrillic, Greek, Arabic, Indic, etc."
        echo "        Precomputed (GPU): CharsiuG2P (zh/ko/yue/gan/wuu), Phonikud (he)"
        echo "        → Run -precompute-phonetics first, then -rebuild-toponyms --resume"
        echo
        echo "      Steps:"
        echo "        1. Extract toponyms from places (with attestations)"
        echo "        2. Filter pre-romanized forms (lang-script mismatches)"
        echo "        3. Generate vocabulary (full Unicode ranges, native script)"
        echo "        4. Compute IPA + PanPhon (Epitran + precomputed neural)"
        echo "        5. Index ALL toponyms to ES (panphon_embedding + name_romanized)"
        echo "        6. Create snapshot"
        echo
        echo "  Options:"
        echo "    --resume                 Resume from existing DuckDB checkpoint"
        echo "    --skip-es-index          Skip ES indexing (extraction + vocab only)"
        echo "    --limit N                Limit places processed (for testing)"
        echo
        echo "  Examples:"
        echo "    $0 -rebuild-toponyms 6                    # Full rebuild v6"
        echo "    $0 -rebuild-toponyms 6 --limit 10000      # Test with subset"
        echo "    $0 -rebuild-toponyms 6 --resume           # Resume with precomputed neural"
        echo
        echo "PRECOMPUTE NEURAL PHONETICS (GPU):"
        echo "  -precompute-phonetics VERSION"
        echo "      Runs CharsiuG2P and Phonikud on GPU for neural-language toponyms."
        echo "      Requires DuckDB from a prior -rebuild-toponyms run."
        echo "      Output: neural_phonetics.parquet (merged on next --resume run)"
        echo
        echo "GENERATE TRAINING DATA (reads from ES toponyms index):"
        echo "  -generate-training-data VERSION [OPTIONS]"
        echo "      Reads PanPhon embeddings from ES to generate training data:"
        echo "        1. Generate positive pairs (HDBSCAN clustering on PanPhon cosine)"
        echo "        2. Balance samples by script+language pair"
        echo "        3. Generate Phase 1 triplets (script-aware random negatives)"
        echo "        4. Generate Phase 2 samples (Student alignment - streaming)"
        echo "        5. Generate Phase 3 triplets (ES KNN hard negatives)"
        echo "        6. Export all to Parquet"
        echo
        echo "  Options:"
        echo "    --force            Force regeneration, ignoring checkpoints"
        echo "    --resume           Resume from checkpoints (default)"
        echo "    --skip-to-phase3   Skip directly to Phase 3 (assumes Phase 1 & 2 complete)"
        echo "    --resume-from-pass2 Resume Phase 3 from Pass 2 (skips ES mining, loads checkpoint)"
        echo
        echo "  Examples:"
        echo "    $0 -generate-training-data 6              # Generate (resume if interrupted)"
        echo "    $0 -generate-training-data 6 --force      # Regenerate from scratch"
        echo "    $0 -generate-training-data 6 --skip-to-phase3  # Jump to Phase 3 only"
        echo "    $0 -generate-training-data 6 --resume-from-pass2  # Resume from Pass 2 after ES restart"
        echo
        echo "MODEL TRAINING PIPELINE:"
        echo "  -train-model VERSION [START_PHASE [END_PHASE]] [OPTIONS]"
        echo "      Submit training job for model version"
        echo "      PHASE: 1 (Teacher), 2 (Student), 3 (Fine-tune)"
        echo "      Omit PHASE to run all three sequentially"
        echo
        echo "  Options:"
        echo "    --resume             Auto-detect and resume from latest epoch checkpoint"
        echo "    --resume-from PATH   Resume from specific checkpoint file"
        echo "    --partition NAME     Use specific GPU partition (a100 or l40s, default: a100)"
        echo
        echo "  Examples:"
        echo "    $0 -train-model 6                       # Train all phases from scratch"
        echo "    $0 -train-model 6 1 --resume            # Resume Phase 1 from latest checkpoint"
        echo "    $0 -train-model 6 1 3 --partition l40s  # Train P1-P3 on L40S GPU"
        echo "    $0 -train-model 6 --resume-from /path/to/phase1_epoch_10.pt"
        echo
        echo "FULL PIPELINE (train + embeddings + index):"
        echo "  -train-and-update VERSION"
        echo "      Chains all jobs: Train (P1→P2→P3) → Compute embeddings → Create ES index"
        echo "      Creates snapshot 'toponyms_vN' when complete"
        echo
        echo "EMBEDDING / INDEX PIPELINE (run AFTER training completes):"
        echo "  -update-embeddings VERSION"
        echo "      Compute embeddings for ALL toponyms from DuckDB (GPU)"
        echo "      Note: Does NOT index - run -update-embeddings-index after compute finishes"
        echo
        echo "  -update-embeddings-index VERSION"
        echo "      Index embeddings to ES (CPU, run after compute completes)"
        echo "      Rebuilds full ES toponyms index from DuckDB + embeddings"
        echo "      Creates snapshot 'toponyms_vN' when complete"
        echo
        echo "  Workflow:"
        echo "    1. es -update-embeddings 6        # Compute embeddings (GPU, ~2-4 hours)"
        echo "    2. es -update-embeddings-index 6  # Index to ES (CPU, ~1-2 hours)"
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
        echo "WORKFLOW (recommended):"
        echo "  1. Start staging ES:         source es.sh -staging-start --places-only"
        echo "  2. Extract + Epitran:        es -rebuild-toponyms 6"
        echo "  3. Neural G2P (GPU):         es -precompute-phonetics 6"
        echo "  4. Merge + index (resume):   es -rebuild-toponyms 6 --resume"
        echo "  5. Generate training:        es -generate-training-data 6"
        echo "  6. Train model:              es -train-model 6"
        echo "  7a. Compute embeddings:      es -update-embeddings 6"
        echo "  7b. Index embeddings:        es -update-embeddings-index 6"
        echo
        echo "Data directory: /ix1/ishi/models/phonetic/data/vN/"
        echo
        echo "NOTES:"
        echo "  - Staging: one instance at a time (port $STAGING_ES_PORT)"
        echo "  - Staging: snapshots must be created explicitly"
        echo "  - Production data: $PROD_DATA_DIR"
        echo "  - Snapshots: $SNAPSHOT_DIR"
        ;;
esac

