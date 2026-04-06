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
    if [ -f "$ELASTIC_PASS_FILE" ]; then
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
    # Restarts ES with security enabled, then uses the
    # elasticsearch-reset-password tool to set passwords for
    # built-in users (no existing credentials required).

    echo "=========================================="
    echo "SECURITY SETUP"
    echo "=========================================="
    echo

    # Check certs exist
    if [ ! -f "${SSL_CERT:-}" ] || [ ! -f "${SSL_KEY:-}" ]; then
        echo "ERROR: TLS certificates not found."
        echo "  Expected: ${SSL_CERT}"
        echo "  Run certbot first."
        return 1
    fi
    echo "✓ TLS certificates found: ${SSL_CERT}"
    echo

    CONFIG_DIR="${IX1_BASE}/es/config"
    mkdir -p "$CONFIG_DIR"
    KIBANA_PASS_FILE="${CONFIG_DIR}/kibana_system.password"
    ELASTIC_PASS_FILE="${CONFIG_DIR}/elastic.password"

    # Step 1: Restart ES with security enabled (certs trigger this automatically)
    echo "Restarting Elasticsearch with security enabled..."
    stop_prod_es
    sleep 3
    start_prod_es
    echo

    # Verify ES is responding (may need auth now)
    if ! curl -s -o /dev/null -w '' "${PROD_ES_URL}/_cluster/health" 2>/dev/null && \
       ! curl -s -o /dev/null -w '' -u "elastic:changeme" "${PROD_ES_URL}/_cluster/health" 2>/dev/null; then
        # Even a 401 means ES is up — check for that
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${PROD_ES_URL}/_cluster/health" 2>/dev/null)
        if [ "$HTTP_CODE" != "401" ] && [ "$HTTP_CODE" != "200" ]; then
            echo "ERROR: ES not responding at ${PROD_ES_URL} (HTTP ${HTTP_CODE})"
            return 1
        fi
    fi
    echo "✓ Elasticsearch is running with security enabled"
    echo

    # Step 2: Reset elastic password using the CLI tool (uses local keystore, no auth needed)
    echo "Resetting 'elastic' superuser password..."
    ELASTIC_PASSWORD=$("$ES_HOME/bin/elasticsearch-reset-password" -u elastic -b -s 2>&1)
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to reset elastic password:"
        echo "$ELASTIC_PASSWORD"
        return 1
    fi
    echo "$ELASTIC_PASSWORD" > "$ELASTIC_PASS_FILE"
    chmod 600 "$ELASTIC_PASS_FILE"
    echo "✓ elastic password saved to ${ELASTIC_PASS_FILE}"
    echo

    # Step 3: Reset kibana_system password
    echo "Resetting 'kibana_system' password..."
    KIBANA_PASSWORD=$("$ES_HOME/bin/elasticsearch-reset-password" -u kibana_system -b -s 2>&1)
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to reset kibana_system password:"
        echo "$KIBANA_PASSWORD"
        return 1
    fi
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
    echo "  1. Start Kibana:  es -kibana-start"
    echo "  2. Log in to Kibana at:            https://${KIBANA_PUBLIC_HOST:-kibana.whgazetteer.org}:5601"
    echo "     Username: elastic"
    echo "     Password: ${ELASTIC_PASSWORD}"
    echo
    echo "  Store the elastic password somewhere safe."
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

    # Check if Gateway is running
    if [ -f "$GATEWAY_PID" ] && kill -0 $(cat "$GATEWAY_PID") 2>/dev/null; then
        echo "Gateway: RUNNING (PID: $(cat $GATEWAY_PID)) on port ${GATEWAY_PORT:-9200}"
    else
        echo "Gateway: STOPPED"
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
    local stopped=false

    # Kill by PID file (the nohup parent)
    if [ -f "$PROD_ES_PID" ]; then
        local pid=$(cat "$PROD_ES_PID")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping Elasticsearch (PID: $pid)..."
            kill "$pid" 2>/dev/null
        fi
        rm -f "$PROD_ES_PID"
        stopped=true
    fi

    # Also kill any Java ES processes (the actual JVM child)
    local es_pids=$(pgrep -f 'org.elasticsearch.bootstrap.Elasticsearch' 2>/dev/null)
    if [ -n "$es_pids" ]; then
        echo "Stopping Elasticsearch JVM process(es): $es_pids"
        kill $es_pids 2>/dev/null
        stopped=true
    fi

    if $stopped; then
        # Wait for graceful shutdown
        echo -n "Waiting for shutdown..."
        for i in {1..15}; do
            if ! pgrep -f 'org.elasticsearch.bootstrap.Elasticsearch' > /dev/null 2>&1; then
                echo " done."
                echo "Elasticsearch stopped."
                return 0
            fi
            echo -n "."
            sleep 2
        done
        # Force kill if still running
        echo " force killing."
        pkill -9 -f 'org.elasticsearch.bootstrap.Elasticsearch' 2>/dev/null
        sleep 2
        echo "Elasticsearch stopped."
    else
        echo "Elasticsearch is not running (no PID file, no process found)."
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

    # Build Kibana args
    # TLS is terminated by the DO reverse proxy — Kibana serves plain HTTP.
    # Auth to ES is needed when xpack.security is enabled.
    KIBANA_EXTRA_ARGS="--server.publicBaseUrl=https://${KIBANA_PUBLIC_HOST:-kibana.whgazetteer.org}
        --elasticsearch.hosts=${PROD_ES_URL}"

    KIBANA_PASS_FILE="${IX1_BASE}/es/config/kibana_system.password"
    ELASTIC_PASS_FILE="${IX1_BASE}/es/config/elastic.password"
    if [ -f "$KIBANA_PASS_FILE" ] && [ -f "$ELASTIC_PASS_FILE" ]; then
        echo "  ES security credentials found - Kibana will authenticate to ES"
        KIBANA_PASSWORD=$(cat "$KIBANA_PASS_FILE")
        KIBANA_EXTRA_ARGS="$KIBANA_EXTRA_ARGS
            --elasticsearch.username=kibana_system
            --elasticsearch.password=${KIBANA_PASSWORD}"
    else
        echo "  No ES security credentials - Kibana connects without auth"
        echo "  Run es -setup-security to enable authentication"
    fi

    nohup "$KIBANA_HOME/bin/kibana" \
        --server.host="0.0.0.0" \
        --path.data="${IX1_BASE}/kibana/data" \
        ${KIBANA_EXTRA_ARGS} \
        > "${IX1_BASE}/kibana/logs/nohup.out" 2>&1 &

    echo $! > "$KIBANA_PID"
    echo "Kibana started (PID: $(cat $KIBANA_PID))"
    echo "Access at: https://${KIBANA_PUBLIC_HOST:-kibana.whgazetteer.org} (via reverse proxy)"
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
# API GATEWAY (VM)
# =============================================================================

GATEWAY_PID="${IX1_BASE}/gateway/gateway.pid"
GATEWAY_LOG_DIR="${IX1_BASE}/gateway/logs"

start_gateway() {
    if [ -f "$GATEWAY_PID" ] && kill -0 $(cat "$GATEWAY_PID") 2>/dev/null; then
        echo "Gateway already running (PID: $(cat $GATEWAY_PID))"
        return 0
    fi

    echo "Starting API Gateway on ${GATEWAY_HOST:-0.0.0.0}:${GATEWAY_PORT:-9200}..."
    mkdir -p "$GATEWAY_LOG_DIR" "$(dirname $GATEWAY_PID)"

    cd "$REPO_DIR"
    nohup python -m gateway \
        > "$GATEWAY_LOG_DIR/nohup.out" 2>&1 &

    echo $! > "$GATEWAY_PID"
    echo "Gateway started (PID: $(cat $GATEWAY_PID))"

    # Wait for gateway
    echo -n "Waiting for gateway..."
    for i in {1..15}; do
        if curl -s "http://localhost:${GATEWAY_PORT:-9200}/api/health" > /dev/null 2>&1; then
            echo " ready!"
            return 0
        fi
        echo -n "."
        sleep 2
    done
    echo " timeout (check $GATEWAY_LOG_DIR/nohup.out)"
}

stop_gateway() {
    if [ -f "$GATEWAY_PID" ]; then
        local pid=$(cat "$GATEWAY_PID")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping Gateway (PID: $pid)..."
            kill "$pid"
            sleep 3
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null
            fi
        fi
        rm -f "$GATEWAY_PID"
        echo "Gateway stopped."
    else
        echo "Gateway is not running (no PID file)."
    fi
}

# =============================================================================
# STAGING ELASTICSEARCH (Slurm)
# =============================================================================

staging_start() {
    # Parse arguments
    local PLACES_ONLY=false
    local NO_SNAPSHOT=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --places-only)
                PLACES_ONLY=true
                shift
                ;;
            --no-snapshot)
                NO_SNAPSHOT=true
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
    if $NO_SNAPSHOT; then
        echo "  Mode: no-snapshot (empty indices will be created)"
    fi

    # Ensure log directory exists
    mkdir -p "$STAGING_SLURM_LOGS"

    # Pass flags via sbatch --export
    local EXPORT_VARS="ALL"
    if $PLACES_ONLY; then
        EXPORT_VARS="${EXPORT_VARS},RESTORE_PLACES_ONLY=1"
    fi
    if $NO_SNAPSHOT; then
        EXPORT_VARS="${EXPORT_VARS},SKIP_SNAPSHOT_RESTORE=1"
    fi

    JOBID=$(sbatch --parsable --export="$EXPORT_VARS" "$STAGING_SCRIPT")

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

# =============================================================================
# CLUSTER EXCHANGE HELPERS (snapshot handoff between prod ↔ staging)
# =============================================================================

# Register the cluster_exchange snapshot repo on a given ES instance.
_ensure_exchange_repo() {
    local es_url="$1"          # e.g. http://localhost:9201
    local curl_cmd="${2:-curl}" # "es_curl" for production, "curl" for staging

    mkdir -p "${CLUSTER_EXCHANGE_DIR}"
    local resp
    resp=$($curl_cmd -s -X PUT "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}" \
        -H 'Content-Type: application/json' -d "{
      \"type\": \"fs\",
      \"settings\": { \"location\": \"${CLUSTER_EXCHANGE_DIR}\" }
    }" 2>&1)
    if echo "$resp" | grep -q '"acknowledged"' 2>/dev/null; then
        return 0
    fi
    echo "  ERROR registering exchange repo on ${es_url}:" >&2
    echo "  $resp" >&2
    return 1
}

# Resolve alias names to their concrete backing indices.
# Outputs a comma-separated list of concrete index names.
# Any name that is already a concrete index is passed through unchanged.
_resolve_aliases() {
    local es_url="$1"
    local aliases="$2"          # comma-separated alias (or index) names
    local curl_cmd="${3:-curl}"

    $curl_cmd -s "${es_url}/_alias/${aliases}" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(','.join(sorted(data.keys())))
except:
    # If _alias endpoint fails, fall back to the original names
    print('${aliases}')
"
}

# Take a snapshot of specific indices. Starts async, then polls until done.
# (Avoids gateway read-timeout on long-running snapshots.)
_take_snapshot() {
    local es_url="$1"
    local snap_name="$2"
    local indices="$3"         # comma-separated
    local curl_cmd="${4:-curl}"

    echo "  Snapshotting ${indices} → ${CLUSTER_EXCHANGE_REPO}/${snap_name} ..."
    echo "  (this may take several minutes for large indices)"

    # Start snapshot asynchronously (no wait_for_completion)
    local resp
    resp=$($curl_cmd -s -X PUT \
        "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}" \
        -H 'Content-Type: application/json' -d "{
      \"indices\": \"${indices}\",
      \"ignore_unavailable\": true,
      \"include_global_state\": false
    }" 2>&1)
    if ! echo "$resp" | grep -q '"accepted"' 2>/dev/null; then
        echo "  ERROR: Snapshot request rejected:" >&2
        echo "  $resp" | head -20 >&2
        return 1
    fi

    # Poll until snapshot completes
    echo -n "  Waiting ..."
    while true; do
        sleep 10
        local state
        state=$($curl_cmd -s \
            "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}" 2>/dev/null \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['snapshots'][0]['state'])" 2>/dev/null || echo "UNKNOWN")
        case "$state" in
            SUCCESS)
                echo " done"
                echo "  ✓ Snapshot complete"
                return 0
                ;;
            FAILED|PARTIAL|MISSING|UNKNOWN)
                echo " $state"
                echo "  ERROR: Snapshot ended in state: $state" >&2
                $curl_cmd -s "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}" 2>/dev/null \
                    | python3 -m json.tool 2>/dev/null | head -30 >&2
                return 1
                ;;
            *)  # IN_PROGRESS, STARTED, etc.
                echo -n "."
                ;;
        esac
    done
}

# Restore a snapshot. Does NOT block; caller should poll _wait_for_restore.
_restore_snapshot() {
    local es_url="$1"
    local snap_name="$2"
    local indices="$3"         # comma-separated (empty = all in snapshot)
    local curl_cmd="${4:-curl}"
    local extra_body="${5:-}"   # extra JSON fields (rename_pattern etc.)

    local body="{
      \"ignore_unavailable\": true,
      \"include_global_state\": false"
    [ -n "$indices" ] && body+=", \"indices\": \"${indices}\""
    [ -n "$extra_body" ] && body+=", ${extra_body}"
    body+="}"

    echo "  Restoring ${CLUSTER_EXCHANGE_REPO}/${snap_name} ..."
    local resp
    resp=$($curl_cmd -s -X POST \
        "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}/_restore" \
        -H 'Content-Type: application/json' -d "$body" 2>&1)
    if echo "$resp" | grep -q '"accepted"' 2>/dev/null; then
        return 0
    fi
    echo "  ERROR: Restore failed:" >&2
    echo "  $resp" | head -20 >&2
    return 1
}

# Block until all shard recovery on an ES instance finishes.
_wait_for_restore() {
    local es_url="$1"
    local curl_cmd="${2:-curl}"

    echo -n "  Waiting for restore to complete"
    while true; do
        local recovering
        recovering=$($curl_cmd -s "${es_url}/_cat/recovery?active_only=true" 2>/dev/null | wc -l)
        [ "$recovering" -eq 0 ] && break
        echo -n "."
        sleep 5
    done
    echo " done"
}

# =============================================================================
# PLACE CLUSTERING
# =============================================================================

do_cluster() {
    # Usage: es -cluster [OPTIONS]
    #
    # Run the WHG place clustering pipeline: entity resolution across
    # authority records via hard links, toponym co-attestation, and
    # Symphonym phonetic similarity.
    #
    # Two execution modes:
    #   (a) Default (on Pitt VM): runs under nohup against localhost ES.
    #   (b) --slurm (from CRC login node): snapshots production indices
    #       into the staging ES on Slurm, runs clustering there, then
    #       use  es -cluster-finalize  to push results back to production.
    #
    # Options are passed through to clustering.runner (see --help).
    # ES host and password are injected automatically.
    #
    # Examples:
    #   es -cluster --full                  # nohup on VM
    #   es -cluster --full --slurm          # snapshot → staging → Slurm job
    #   es -cluster --incremental           # nohup on VM
    #   es -cluster --full --dry-run        # nohup, no indexing
    #   es -cluster --stats                 # quick query (no nohup)
    #   es -cluster --full --slurm --mem 300G --time 24:00:00

    # --- Parse our flags (--slurm, --mem, --time); rest goes to Python ---
    local USE_SLURM=false
    local SLURM_MEM="500G"
    local SLURM_TIME="3-00:00:00"
    local PYTHON_ARGS=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --slurm)    USE_SLURM=true; shift ;;
            --mem)      SLURM_MEM="$2"; shift 2 ;;
            --time)     SLURM_TIME="$2"; shift 2 ;;
            *)          PYTHON_ARGS+=("$1"); shift ;;
        esac
    done

    # --resume implies --full (it resumes a previous full run).
    # Add --full automatically if no mode flag was given.
    local HAS_MODE=false
    for _a in "${PYTHON_ARGS[@]}"; do
        case "$_a" in --full|--incremental|--stats) HAS_MODE=true ;; esac
    done
    if ! $HAS_MODE; then
        # Default to --full; if --resume is present the user clearly
        # wants to continue a full run.
        PYTHON_ARGS=("--full" "${PYTHON_ARGS[@]}")
    fi

    local PASS_FILE="${IX1_BASE}/es/config/elastic.password"
    local LOG_DIR="${IX1_BASE}/es/logs"
    local TIMESTAMP
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)

    mkdir -p "$LOG_DIR"

    # Build the full python command as a flat string — avoids heredoc
    # quoting pitfalls with arrays and line-continuations.
    local PYTHON_CMD="python -u -m clustering.runner"

    # ---- Slurm mode: snapshot prod → staging, run on Slurm ----
    if $USE_SLURM; then
        # From a CRC login node, production ES is reachable via the gateway
        # (localhost:9201 is only valid on the VM itself).
        local PROD_URL="http://gazetteer.crcd.pitt.edu:${GATEWAY_PORT:-9200}"
        local SNAP_NAME="cluster_input_${TIMESTAMP}"

        echo "=========================================="
        echo "PLACE CLUSTERING (Slurm via staging ES)"
        echo "=========================================="
        echo "Args:     ${PYTHON_ARGS[*]}"
        echo "Mem:      ${SLURM_MEM}"
        echo "Time:     ${SLURM_TIME}"
        echo

        # 1. Verify production ES is reachable (from login node / VM)
        echo "Step 1: Checking production ES at ${PROD_URL} ..."
        if ! es_curl --connect-timeout 10 "${PROD_URL}/_cluster/health" &>/dev/null; then
            echo "ERROR: Cannot reach production ES at ${PROD_URL}"
            return 1
        fi
        echo "  ✓ Production ES reachable"
        echo

        # 2. Ensure staging ES is running (auto-start if needed)
        echo "Step 2: Checking staging ES ..."
        local NEED_STAGING=false
        if [ ! -f "$STAGING_INFO_FILE" ]; then
            NEED_STAGING=true
        else
            source "$STAGING_INFO_FILE"
            if ! curl -sf --connect-timeout 10 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
                echo "  Staging ES at http://${ES_NODE}:${ES_PORT} is not responding (job may have expired)."
                NEED_STAGING=true
            fi
        fi
        if $NEED_STAGING; then
            echo "  No running staging ES found — starting one automatically ..."
            staging_start
            if [ ! -f "$STAGING_INFO_FILE" ]; then
                echo "  ERROR: Failed to auto-start staging ES."
                echo "  Try manually:  es -staging-start"
                return 1
            fi
            source "$STAGING_INFO_FILE"
        fi
        local STAGING_URL="http://${ES_NODE}:${ES_PORT}"
        if ! curl -sf --connect-timeout 10 "${STAGING_URL}/_cluster/health" &>/dev/null; then
            echo "  ERROR: Staging ES at ${STAGING_URL} is still not responding after auto-start."
            return 1
        fi
        echo "  ✓ Staging ES reachable at ${STAGING_URL}"
        echo

        # Check whether we can skip snapshot/restore (--resume with data already on staging)
        local SKIP_SNAPSHOT=false
        if printf '%s\n' "${PYTHON_ARGS[@]}" | grep -qx -- '--resume'; then
            if curl -sf "${STAGING_URL}/places/_count" >/dev/null 2>&1; then
                SKIP_SNAPSHOT=true
                echo "  --resume: places index already on staging, skipping snapshot/restore"
                echo "  Index status on staging:"
                curl -sf "${STAGING_URL}/_cat/indices?v&h=index,docs.count,store.size" 2>/dev/null || true
                echo
            fi
        fi

        if ! $SKIP_SNAPSHOT; then

        # 3. Register exchange repo on both instances
        echo "Step 3: Registering snapshot exchange repo ..."
        _ensure_exchange_repo "$PROD_URL" es_curl
        echo "  ✓ Production"
        _ensure_exchange_repo "$STAGING_URL" curl
        echo "  ✓ Staging"
        echo

        # 4. Resolve aliases and snapshot production input indices
        echo "Step 4: Snapshotting production indices ..."
        # 'places' and 'toponyms' are aliases pointing to versioned concrete
        # indices (e.g. places_20250320).  The snapshot API requires concrete
        # index names — alias names are silently ignored.
        local CONCRETE_INDICES
        CONCRETE_INDICES=$(_resolve_aliases "$PROD_URL" "places,toponyms" es_curl)
        echo "  Resolved aliases → concrete indices: ${CONCRETE_INDICES}"
        if [ -z "$CONCRETE_INDICES" ] || [ "$CONCRETE_INDICES" = "places,toponyms" ]; then
            # Fallback: if _alias returned the same names, try a wildcard approach
            CONCRETE_INDICES=$(es_curl -s "${PROD_URL}/_cat/indices/places*,toponyms*?h=index" 2>/dev/null \
                | tr '\n' ',' | sed 's/,$//')
            echo "  (wildcard fallback: ${CONCRETE_INDICES})"
        fi
        if [ -z "$CONCRETE_INDICES" ]; then
            echo "  ERROR: Could not resolve any concrete indices for places,toponyms" >&2
            return 1
        fi
        _take_snapshot "$PROD_URL" "$SNAP_NAME" "$CONCRETE_INDICES" es_curl
        echo

        # 5. Delete any pre-existing copies on staging, then restore
        echo "Step 5: Restoring input indices into staging ES ..."
        # Delete both aliases and concrete indices that might exist
        for idx in places toponyms $(echo "$CONCRETE_INDICES" | tr ',' ' '); do
            curl -s -X DELETE "${STAGING_URL}/${idx}" >/dev/null 2>&1 || true
        done
        # Force staging ES to rediscover snapshots: DELETE the repo
        # registration then re-register.  A simple PUT re-registration
        # is not sufficient — ES may serve its cached repo metadata
        # without re-reading the shared filesystem.
        curl -s -X DELETE "${STAGING_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}" >/dev/null 2>&1 || true
        _ensure_exchange_repo "$STAGING_URL" curl
        _restore_snapshot "$STAGING_URL" "$SNAP_NAME" "$CONCRETE_INDICES" curl
        _wait_for_restore "$STAGING_URL" curl

        # Staging is single-node so replica shards can never be assigned.
        # Set replicas to 0 on the restored indices to avoid a permanently
        # yellow cluster that never reaches green.
        echo "  Setting number_of_replicas=0 on restored indices ..."
        for idx in $(echo "$CONCRETE_INDICES" | tr ',' ' '); do
            curl -s -X PUT "${STAGING_URL}/${idx}/_settings" \
                -H 'Content-Type: application/json' \
                -d '{"index":{"number_of_replicas":0}}' >/dev/null 2>&1 || true
        done

        # Wait for cluster health to reach green (all shards assigned).
        # With replicas=0 this means only primaries need to finish initialising.
        echo -n "  Waiting for index shards to initialise ..."
        for _i in $(seq 1 360); do  # up to 60 minutes
            local HEALTH
            HEALTH=$(curl -sf "${STAGING_URL}/_cluster/health" 2>/dev/null \
                | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','red'))" 2>/dev/null || echo "red")
            if [ "$HEALTH" = "green" ]; then
                break
            fi
            echo -n "."
            sleep 10
        done
        echo " done"

        # Re-create aliases on staging so the clustering code can find
        # the indices by their canonical names (places, toponyms)
        echo "  Creating aliases on staging ..."
        for ALIAS_NAME in places toponyms; do
            # Find the concrete index matching this alias prefix
            local CONCRETE
            CONCRETE=$(echo "$CONCRETE_INDICES" | tr ',' '\n' \
                | grep -E "^${ALIAS_NAME}(_|$)" | head -1)
            if [ -n "$CONCRETE" ] && [ "$CONCRETE" != "$ALIAS_NAME" ]; then
                curl -s -X POST "${STAGING_URL}/_aliases" \
                    -H 'Content-Type: application/json' -d "{
                  \"actions\": [{\"add\": {\"index\": \"${CONCRETE}\", \"alias\": \"${ALIAS_NAME}\"}}]
                }" >/dev/null 2>&1
                echo "    ${ALIAS_NAME} → ${CONCRETE}"
            fi
        done
        echo "  Index status on staging:"
        curl -sf "${STAGING_URL}/_cat/indices?v&h=index,docs.count,store.size" 2>/dev/null || true
        echo

        fi  # end SKIP_SNAPSHOT

        # 6. Build python command (no auth — staging has xpack.security off)
        PYTHON_CMD+=" --es-host ${STAGING_URL} ${PYTHON_ARGS[*]}"
        local OUTPUT_SNAP="cluster_output_${TIMESTAMP}"

        echo "Step 6: Submitting clustering Slurm job ..."
        echo "  Command: ${PYTHON_CMD}"

        local SLURM_SCRIPT
        SLURM_SCRIPT=$(mktemp /tmp/cluster-XXXXXX.sbatch)

        cat > "$SLURM_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=whg-cluster
#SBATCH --output=${LOG_DIR}/cluster_%j.out
#SBATCH --error=${LOG_DIR}/cluster_%j.err
#SBATCH --time=${SLURM_TIME}
#SBATCH --partition=htc
#SBATCH --qos=normal
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=${SLURM_MEM}

set -e

echo "=========================================="
echo "PLACE CLUSTERING (Slurm → staging ES)"
echo "=========================================="
echo "Started: \$(date)"
echo "Node:    \$(hostname)"
echo "ES:      ${STAGING_URL} (staging)"
echo

# Verify staging ES is still alive and indices are ready
echo "Checking staging ES readiness ..."
if ! curl -sf --connect-timeout 10 "${STAGING_URL}/_cluster/health" >/dev/null 2>&1; then
    echo "ERROR: Staging ES at ${STAGING_URL} is not responding." >&2
    echo "  The staging Slurm job may have expired." >&2
    exit 1
fi
echo "  ES is responding. Waiting for indices to be ready ..."
# Poll until the places alias/index is queryable (up to 60 minutes).
# The login-node Step 5 already set replicas=0 and created aliases,
# but shards may still be initialising from the snapshot restore.
for ATTEMPT in \$(seq 1 360); do
    if curl -sf "${STAGING_URL}/places/_count" >/dev/null 2>&1; then
        echo "  ✓ Staging ES indices are ready"
        break
    fi
    if [ "\$ATTEMPT" -eq 360 ]; then
        echo "ERROR: places index not ready after 60 minutes." >&2
        echo "  Cluster health:" >&2
        curl -sf "${STAGING_URL}/_cluster/health?pretty" >&2 || true
        exit 1
    fi
    [ \$((\$ATTEMPT % 6)) -eq 0 ] && echo "    ... still waiting (\$((ATTEMPT/6)) min)"
    sleep 10
done

# Load environment
source "${CONDA_SETUP_PATH}"
conda activate whg
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"
cd "${REPO_DIR}"

${PYTHON_CMD}

echo
echo "Clustering finished. Snapshotting output indices ..."
curl -sf -X PUT "${STAGING_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${OUTPUT_SNAP}?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
  "indices": "clusters,cluster_state",
  "ignore_unavailable": true,
  "include_global_state": false
}'
echo
echo "=========================================="
echo "CLUSTERING COMPLETE"
echo "=========================================="
echo "Output snapshot: ${CLUSTER_EXCHANGE_REPO}/${OUTPUT_SNAP}"
echo "Finished: \$(date)"
echo
echo "Next step — push results to production:"
echo "  es -cluster-finalize ${TIMESTAMP}"
SBATCH_EOF

        JOBID=$(sbatch --parsable "$SLURM_SCRIPT")
        rm -f "$SLURM_SCRIPT"
        CLEAN_JOBID="${JOBID%;*}"

        echo
        echo "✓ Clustering job submitted: ${CLEAN_JOBID}"
        echo
        echo "Monitor with:"
        echo "  squeue -j ${CLEAN_JOBID}"
        echo "  tail -f ${LOG_DIR}/cluster_${CLEAN_JOBID}.out"
        echo
        echo "After the job completes, push results to production with:"
        echo "  es -cluster-finalize ${TIMESTAMP}"
        echo
        return 0
    fi

    # ---- nohup mode (on VM) ----
    local ES_URL="${PROD_ES_URL:-http://localhost:${PROD_ES_INTERNAL_PORT:-9201}}"
    local LOG_FILE="${LOG_DIR}/cluster_${TIMESTAMP}.log"

    [ -f "$PASS_FILE" ] && PYTHON_CMD+=" --es-pass-file ${PASS_FILE}"
    PYTHON_CMD+=" --es-host ${ES_URL} ${PYTHON_ARGS[*]}"

    # Verify ES is responding
    if ! es_curl --connect-timeout 5 "${ES_URL}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to production ES at ${ES_URL}"
        return 1
    fi

    echo "=========================================="
    echo "PLACE CLUSTERING (nohup)"
    echo "=========================================="
    echo "ES:      ${ES_URL}"
    echo "Args:    ${PYTHON_ARGS[*]}"
    echo "Command: ${PYTHON_CMD}"
    echo "Log:     ${LOG_FILE}"
    echo

    # Write a small wrapper script so nohup runs in the right env
    local WRAPPER
    WRAPPER=$(mktemp /tmp/cluster-XXXXXX.sh)

    cat > "$WRAPPER" <<WRAPPER_EOF
#!/bin/bash
# Activate conda — try known paths, fall back to conda already in PATH
for _cs in "${CONDA_SETUP_PATH}" \\
           "\$HOME/miniconda3/etc/profile.d/conda.sh" \\
           "\$HOME/anaconda3/etc/profile.d/conda.sh"; do
    [ -f "\$_cs" ] && source "\$_cs" && break
done

conda activate whg 2>/dev/null || true
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"
cd "${REPO_DIR}"

${PYTHON_CMD}
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

do_cluster_finalize() {
    # Usage: es -cluster-finalize TIMESTAMP
    #
    # After a Slurm clustering job completes, this restores the output
    # snapshot (clusters, cluster_state) into production ES and performs
    # an atomic alias swap so production queries see the new data with
    # zero downtime.
    #
    # The TIMESTAMP is printed by  es -cluster --full --slurm  on submission.

    local TIMESTAMP="$1"
    if [ -z "$TIMESTAMP" ]; then
        echo "Usage: es -cluster-finalize TIMESTAMP"
        echo
        echo "TIMESTAMP was printed when you ran:  es -cluster --full --slurm"
        echo "It identifies the snapshot pair (cluster_input_TS / cluster_output_TS)."
        return 1
    fi

    # Detect production ES URL: localhost if on the VM, gateway otherwise
    local PROD_URL="${PROD_ES_URL:-http://localhost:${PROD_ES_INTERNAL_PORT:-9201}}"
    if ! es_curl --connect-timeout 3 "${PROD_URL}/_cluster/health" &>/dev/null; then
        PROD_URL="http://gazetteer.crcd.pitt.edu:${GATEWAY_PORT:-9200}"
    fi
    local SNAP_NAME="cluster_output_${TIMESTAMP}"

    echo "=========================================="
    echo "CLUSTER FINALIZE"
    echo "=========================================="
    echo "Production ES: ${PROD_URL}"
    echo "Snapshot:      ${CLUSTER_EXCHANGE_REPO}/${SNAP_NAME}"
    echo

    # 1. Verify production ES
    echo "Step 1: Checking production ES ..."
    if ! es_curl --connect-timeout 10 "${PROD_URL}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot reach production ES at ${PROD_URL}"
        return 1
    fi
    echo "  ✓ Production ES reachable"
    echo

    # 2. Register exchange repo (idempotent) and verify snapshot exists
    echo "Step 2: Verifying output snapshot exists ..."
    # Force production ES to rediscover snapshots created by staging
    es_curl -s -X DELETE "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}" >/dev/null 2>&1 || true
    _ensure_exchange_repo "$PROD_URL" es_curl

    local SNAP_STATE
    SNAP_STATE=$(es_curl -sf "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${SNAP_NAME}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['snapshots'][0]['state'])" 2>/dev/null || echo "MISSING")
    if [ "$SNAP_STATE" != "SUCCESS" ]; then
        echo "  ERROR: Snapshot ${SNAP_NAME} not found or not successful (state: ${SNAP_STATE})"
        echo "  Has the Slurm clustering job finished?"
        return 1
    fi
    echo "  ✓ Snapshot exists and is SUCCESS"
    echo

    # 3. Restore into dated indices (e.g. clusters_20260320, cluster_state_20260320)
    local DATE_SUFFIX
    DATE_SUFFIX=$(echo "$TIMESTAMP" | cut -c1-8)  # YYYYMMDD
    local NEW_CLUSTERS="clusters_${DATE_SUFFIX}"
    local NEW_STATE="cluster_state_${DATE_SUFFIX}"

    echo "Step 3: Restoring into dated indices ..."
    echo "  clusters      → ${NEW_CLUSTERS}"
    echo "  cluster_state → ${NEW_STATE}"

    # Delete targets if they already exist (e.g. re-running finalize)
    for idx in "$NEW_CLUSTERS" "$NEW_STATE"; do
        es_curl -sf -X DELETE "${PROD_URL}/${idx}" >/dev/null 2>&1 || true
    done

    local RENAME_BODY="\"rename_pattern\": \"(.+)\", \"rename_replacement\": \"\$1_${DATE_SUFFIX}\""
    _restore_snapshot "$PROD_URL" "$SNAP_NAME" "clusters,cluster_state" es_curl "$RENAME_BODY"
    _wait_for_restore "$PROD_URL" es_curl

    echo "  Index status:"
    es_curl -sf "${PROD_URL}/_cat/indices/${NEW_CLUSTERS},${NEW_STATE}?v&h=index,docs.count,store.size" 2>/dev/null || true
    echo

    # 4. Atomic alias swap
    echo "Step 4: Swapping aliases ..."

    # Build the alias actions:
    #   - Remove any existing alias pointing for 'clusters' / 'cluster_state'
    #   - Add alias for the new dated indices
    #   - Also handle the case where a concrete (non-aliased) index exists
    local ACTIONS='{"actions":['

    for pair in "clusters:${NEW_CLUSTERS}" "cluster_state:${NEW_STATE}"; do
        local ALIAS="${pair%%:*}"
        local TARGET="${pair##*:}"

        # Check if the alias currently exists
        local CURRENT_INDEX
        CURRENT_INDEX=$(es_curl -sf "${PROD_URL}/_alias/${ALIAS}" \
            | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).keys()))" 2>/dev/null || echo "")

        if [ -n "$CURRENT_INDEX" ] && [ "$CURRENT_INDEX" != "$TARGET" ]; then
            # Alias exists, pointing elsewhere → remove old, add new
            ACTIONS+='{"remove":{"index":"'"${CURRENT_INDEX}"'","alias":"'"${ALIAS}"'"}},'
            ACTIONS+='{"add":{"index":"'"${TARGET}"'","alias":"'"${ALIAS}"'"}},'
            echo "  ${ALIAS}: ${CURRENT_INDEX} → ${TARGET}"
        elif [ -z "$CURRENT_INDEX" ]; then
            # No alias. Check if a concrete index with that name exists.
            if es_curl -sf -o /dev/null "${PROD_URL}/${ALIAS}"; then
                echo "  ${ALIAS}: concrete index exists — deleting to make room for alias"
                es_curl -sf -X DELETE "${PROD_URL}/${ALIAS}" >/dev/null
            fi
            ACTIONS+='{"add":{"index":"'"${TARGET}"'","alias":"'"${ALIAS}"'"}},'
            echo "  ${ALIAS}: (new) → ${TARGET}"
        else
            echo "  ${ALIAS}: already points to ${TARGET} (no change)"
        fi
    done

    # Strip trailing comma, close
    ACTIONS="${ACTIONS%,}]}"

    if echo "$ACTIONS" | grep -q '"add"'; then
        es_curl -sf -X POST "${PROD_URL}/_aliases" \
            -H 'Content-Type: application/json' -d "$ACTIONS" >/dev/null
        echo "  ✓ Aliases swapped atomically"
    else
        echo "  (no alias changes needed)"
    fi
    echo

    # 5. Summary
    echo "=========================================="
    echo "FINALIZE COMPLETE"
    echo "=========================================="
    echo
    echo "Production aliases:"
    es_curl -sf "${PROD_URL}/_cat/aliases/clusters,cluster_state?v" 2>/dev/null || true
    echo
    echo "You may now clean up old dated indices if desired:"
    es_curl -sf "${PROD_URL}/_cat/indices/clusters_*,cluster_state_*?v&h=index,docs.count,store.size" 2>/dev/null || true
    echo
    echo "Cleaning up exchange snapshots ..."
    es_curl -sf -X DELETE "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/cluster_input_${TIMESTAMP}" >/dev/null 2>&1 \
        && echo "  ✓ Deleted cluster_input_${TIMESTAMP}" \
        || echo "  ⚠ cluster_input_${TIMESTAMP} not found (may already be cleaned)"
    es_curl -sf -X DELETE "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/cluster_output_${TIMESTAMP}" >/dev/null 2>&1 \
        && echo "  ✓ Deleted cluster_output_${TIMESTAMP}" \
        || echo "  ⚠ cluster_output_${TIMESTAMP} not found (may already be cleaned)"
}

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

    # --- Boundary Index Ingestion ---
    -ingest-boundaries)
        shift
        do_ingest_boundaries "$@"
        ;;

    # --- Augment ccodes (spatial country code assignment) ---
    -augment-ccodes)
        shift
        do_augment_ccodes "$@"
        ;;

    # --- Place Clustering (entity resolution across authorities) ---
    -cluster)
        shift
        do_cluster "$@"
        ;;

    -cluster-finalize)
        shift
        do_cluster_finalize "$@"
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
        start_gateway
        ;;
    -stop)
        stop_gateway
        stop_kibana
        stop_prod_es
        ;;
    -restart)
        stop_gateway
        stop_kibana
        stop_prod_es
        sleep 2
        start_prod_es
        start_kibana
        start_gateway
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
    gateway-start)
        start_gateway
        ;;
    gateway-stop)
        stop_gateway
        ;;
    gateway-restart)
        stop_gateway
        sleep 2
        start_gateway
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

    # --- DO Backend Switching ---
    -do-check)
        echo "Checking DO ES backend status..."
        cd "$REPO_DIR"
        python3 scripts/switch_do_es_backend.py --check
        ;;
    -do-switch)
        shift
        target="${1:-}"
        shift 2>/dev/null || true
        if [ -z "$target" ]; then
            echo "Usage: $0 -do-switch <pitt|local> [--dry-run] [--yes]"
            exit 1
        fi
        cd "$REPO_DIR"
        python3 scripts/switch_do_es_backend.py --switch-to "$target" "$@"
        ;;
    -do-revert)
        shift
        cd "$REPO_DIR"
        python3 scripts/switch_do_es_backend.py --revert "$@"
        ;;
    -do-stop-es)
        shift
        cd "$REPO_DIR"
        python3 scripts/switch_do_es_backend.py --stop-do-es "$@"
        ;;
    -do-start-es)
        shift
        cd "$REPO_DIR"
        python3 scripts/switch_do_es_backend.py --start-do-es "$@"
        ;;
    -do-clone)
        shift
        cd "$REPO_DIR"
        python3 scripts/clone_do_indexes.py "$@"
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
        echo "  -ingest-boundaries [OPTIONS]  Extract OSM/OHM admin boundaries into boundaries index"
        echo "    --source osm|ohm|both     Which PBF to process (default: both)"
        echo "    --replace                 Delete existing boundaries first"
        echo "    --no-tiles                Skip .mbtiles generation"
        echo
        echo "  Examples:"
        echo "    $0 -ingest-boundaries                   # Both OSM + OHM"
        echo "    $0 -ingest-boundaries --source osm      # OSM only"
        echo "    $0 -ingest-boundaries --replace         # Re-extract from scratch"
        echo
        echo "AUGMENT CCODES (runs against production ES):"
        echo "  -augment-ccodes [OPTIONS]"
        echo "      Assign ISO 3166-1 alpha-2 country codes to places by spatially"
        echo "      intersecting geometries against full-resolution Natural Earth polygons."
        echo "      Scans places missing ccodes; batched updates with throttling."
        echo
        echo "  Options (passed to processing.augment_ccodes):"
        echo "    --dry-run              Compute matches but do not write to ES"
        echo "    --recompute-all        Process all places, not just those missing ccodes"
        echo "    --namespace NS         Only process a specific namespace (e.g. osm, wd, tgn)"
        echo "    --batch-size N         Bulk update chunk size (default 500)"
        echo "    --throttle SECS        Sleep between bulk flushes (default 0.5)"
        echo "    --limit N              Max documents to scan (for testing)"
        echo "    --snapshot             Create checkpoint snapshot after completion"
        echo "    --no-download          Use already-cached Natural Earth data"
        echo
        echo "  Examples:"
        echo "    $0 -augment-ccodes --dry-run --limit 100    # Test run"
        echo "    $0 -augment-ccodes --namespace tgn           # Process TGN only"
        echo "    $0 -augment-ccodes                           # Process all missing"
        echo "    $0 -augment-ccodes --recompute-all           # Recompute everything"
        echo
        echo "PLACE CLUSTERING (entity resolution across authorities):"
        echo "  -cluster [OPTIONS]"
        echo "      Pre-compute equivalence clusters across authority records."
        echo "      Hard links (authority sameAs, contributor reconciliation),"
        echo "      toponym co-attestation, and Symphonym phonetic similarity."
        echo
        echo "  Execution modes:"
        echo "    Default (on Pitt VM): runs under nohup against localhost ES."
        echo "    --slurm (from CRC login node): snapshots production indices"
        echo "            into staging ES, submits Slurm job, then use"
        echo "            -cluster-finalize to push results back to production."
        echo
        echo "  Options (passed to clustering.runner):"
        echo "    --full               Full initial run (all phases)"
        echo "    --incremental        Incremental run (since last run)"
        echo "    --resume             Resume a crashed --full run from checkpoint"
        echo "    --stats              Show current state and statistics"
        echo "    --dry-run            Compute but don't index results"
        echo "    --max-phase3-places N  Cap un-clustered places in Phase 3 (0=unlimited)"
        echo "    -v, --verbose        Verbose (DEBUG-level) logging"
        echo
        echo "  Slurm options:"
        echo "    --slurm              Submit as Slurm job (requires staging ES running)"
        echo "    --mem SIZE           Slurm memory (default: 500G)"
        echo "    --time HH:MM:SS     Slurm wall time (default: 3-00:00:00)"
        echo
        echo "  Examples:"
        echo "    $0 -cluster --full                    # nohup on VM"
        echo "    $0 -cluster --full --slurm            # snapshot → staging → Slurm"
        echo "    $0 -cluster --full --slurm --mem 750G # Slurm with 750G RAM"
        echo "    $0 -cluster --incremental              # nohup, since last run"
        echo "    $0 -cluster --full --dry-run           # test run"
        echo "    $0 -cluster --stats                    # show statistics"
        echo
        echo "  -cluster-finalize TIMESTAMP"
        echo "      After a Slurm clustering job completes, restore output indices"
        echo "      to production ES with zero-downtime alias swap."
        echo "      TIMESTAMP is printed by -cluster --slurm on submission."
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
        echo "DO MIGRATION (switch DO Django app between local ES and Pitt ES):"
        echo "  -do-check           Show current DO ES backend and connectivity"
        echo "  -do-switch pitt     Switch DO → Pitt ES (interactive)"
        echo "  -do-switch local    Switch DO → local ES (revert)"
        echo "  -do-revert          Alias for -do-switch local"
        echo "  -do-stop-es         Stop bare-metal ES on DO (data preserved)"
        echo "  -do-start-es        Start bare-metal ES on DO"
        echo "  -do-clone [OPTS]    Clone DO indexes to Pitt (see clone_do_indexes.py)"
        echo
        echo "  Examples:"
        echo "    $0 -do-check                     # Check current state"
        echo "    $0 -do-switch pitt --dry-run      # Preview switch to Pitt"
        echo "    $0 -do-switch pitt --yes           # Switch to Pitt (no confirm)"
        echo "    $0 -do-revert --yes                # Revert to local ES"
        echo "    $0 -do-clone --skip-existing       # Clone new indexes to Pitt"
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
        echo "  source $0 -staging-start --no-snapshot  Launch with empty indices (no snapshot restore)"
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

