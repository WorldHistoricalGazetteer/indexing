#!/bin/bash
# =============================================================================
# scripts/es.sh
# WHG Elasticsearch and Kibana management wrapper
# =============================================================================
#
# Single entry point for all WHG infrastructure commands. Sources domain-
# specific modules for Symphonym training, clustering, and ingestion.
set -e

# --- Shared bootstrap (paths, .env, conda, helpers) ---
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# --- Domain modules (define functions used by the case dispatcher below) ---
source "${SCRIPT_DIR}/symphonym.sh"
# cluster.sh removed — Master Plan replaces ES post-index clustering with the
# SQLite hard-link overlay (processing/submit_hardlinks_slurm.py).
source "${SCRIPT_DIR}/ingest.sh"

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

_update_clone() {
    # Reset a single deployment clone to origin/main. Stashes local changes
    # after confirmation. Returns 1 on user-cancelled stash, 2 on missing repo.
    #
    # The /vast clone may be owned by a different user (e.g. stg135 set it up
    # but `es` is invoked as `gazetteer`), so git's dubious-ownership check
    # would otherwise refuse every command. ``-c safe.directory=...`` trusts
    # this one path for the lifetime of each invocation, without polluting
    # the user's global git config.
    local clone_dir="$1"
    local label="$2"

    if [ ! -d "$clone_dir" ]; then
        echo "  ($label) skipped — repository not found at $clone_dir"
        return 2
    fi

    local g=(git -C "$clone_dir" -c "safe.directory=$clone_dir")

    if ! "${g[@]}" diff --quiet 2>/dev/null; then
        echo "  ($label) WARNING: local changes present:"
        "${g[@]}" status --short
        echo
        read -p "  Stash changes in $label clone and continue? (y/n): " confirm
        if [ "$confirm" = "y" ]; then
            "${g[@]}" stash
            echo "  ($label) Changes stashed. Restore later with: git -C $clone_dir stash pop"
        else
            echo "  ($label) Cancelled."
            return 1
        fi
    fi

    echo "  ($label) Fetching latest from main..."
    # Shallow clones (--depth=1) need --depth=1 on the fetch too, otherwise
    # git tries to resolve missing history and bails out.
    if [ -f "$clone_dir/.git/shallow" ]; then
        "${g[@]}" fetch --depth=1 origin main
    else
        "${g[@]}" fetch origin main
    fi

    # Reset to match origin/main exactly (deployment clone should mirror main)
    "${g[@]}" reset --hard origin/main
    echo "  ($label) ✓ Updated to $("${g[@]}" rev-parse --short HEAD)"
    return 0
}

do_update() {
    echo "Updating from git repository..."

    if [ ! -d "$REPO_DIR" ] && [ ! -d "${VAST_REPO_DIR:-}" ]; then
        echo "ERROR: No repository found at $REPO_DIR or ${VAST_REPO_DIR:-<unset>}"
        echo "Run installation first or clone manually."
        return 1
    fi

    # ix1_failed: track failure of the /ix1 clone update so /vast can still
    # proceed when /ix1 NFS is overloaded (the common reason for git
    # operations on /ix1 to hang). Only an explicit user-cancelled stash
    # (rc=1) aborts the whole update — ANY other failure on /ix1 is logged
    # and we move on to /vast, which is the actual hot-path clone.
    local ix1_failed=0
    if [ -d "$REPO_DIR" ]; then
        _update_clone "$REPO_DIR" "/ix1" || {
            local rc=$?
            if [ "$rc" = "1" ]; then
                # User cancelled the stash prompt — respect that.
                return 1
            fi
            echo "  (/ix1) update failed (rc=$rc) — continuing to /vast"
            ix1_failed=1
        }
    fi

    # Operational shallow clone on /vast (Slurm jobs run from here so Python
    # imports and log writes stay off the /ix1 NFS mount). Skipped silently
    # if VAST_REPO_DIR is unset or the directory doesn't exist.
    if [ -n "${VAST_REPO_DIR:-}" ] && [ "$VAST_REPO_DIR" != "$REPO_DIR" ]; then
        _update_clone "$VAST_REPO_DIR" "/vast" || {
            local rc=$?
            [ "$rc" = "1" ] && return 1
            echo "  (/vast) update failed (rc=$rc)"
            # If both clones failed it's a real problem — surface it.
            [ "$ix1_failed" = "1" ] && return 1
        }
    fi

    if [ "$ix1_failed" = "1" ]; then
        echo "⚠ Update partial: /vast updated, /ix1 skipped (NFS likely overloaded)"
    else
        echo "✓ Update complete"
    fi
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

    # JVM heap: default 15g; override per host via ES_HEAP_SIZE in .env.local.
    # This box now has 62g RAM (was 32g) → set ES_HEAP_SIZE=28g, leaving ~34g for
    # the OS page cache / off-heap mmapped vectors. A larger heap avoids the OOM /
    # G1 Full-GC death spiral seen during HNSW dense_vector segment merges of the
    # ~67M-doc toponyms index after heavy indexing (see memory / plan 2026-07-13).
    export ES_JAVA_OPTS="-Xms${ES_HEAP_SIZE:-15g} -Xmx${ES_HEAP_SIZE:-15g}"
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

    -h3-stage)
        shift
        do_h3_stage_array "$@"
        ;;

    # --- Boundary Pass (manual geometry repair / replay) ---
    -boundary-pass)
        shift
        do_boundary_pass "$@"
        ;;

    -generate-tiles)
        shift
        do_generate_tiles "$@"
        ;;

    # Legacy alias
    -ingest-boundaries)
        shift
        do_boundary_pass "$@"
        ;;

    # NOTE: -augment-ccodes / -cluster / -cluster-finalize have been
    # retired. CCode enrichment is now staged (processing/ccode_enrichment.py
    # via processing/submit_ccode_slurm.py); the Master Plan replaces ES
    # post-index clustering with the SQLite hard-link overlay built by
    # processing/submit_hardlinks_slurm.py and queried at search-time.

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
        echo "  -h3-stage [OPTIONS] Submit decoupled staged H3 derivation as a Slurm array"
        echo
        echo "  Ingestion options (passed to ingest_all_authorities.py):"
        echo "    -n, --namespaces NS   Comma-separated list: gn,wd,pl,tgn,gb,un,osm,nl,dp,iv,loc"
        echo "    --skip-existing       Skip authorities already in index"
        echo "    --check-only          Check data availability only"
        echo "    --defer-h3            Mark H3 as pending for separate H3 array stage"
        echo "    --run-id ID           Use explicit run id (needed for chained H3 array)"
        echo "    --submit-h3-array     Wrapper option: submit dependent -h3-stage after ingest"
        echo
        echo "  H3 stage options:"
        echo "    --run-id ID           Required run id to process"
        echo "    --dependency JOBID    Optional Slurm dependency (afterok:JOBID)"
        echo
        echo "  Examples:"
        echo "    $0 -ingest                        # Ingest all authorities"
        echo "    $0 -ingest -n gn,wd               # Ingest GeoNames and Wikidata only"
        echo "    $0 -ingest --skip-existing        # Skip already ingested"
        echo "    $0 -ingest --check-only           # Check what's available"
        echo "    $0 -ingest --defer-h3 --submit-h3-array"
        echo "    $0 -h3-stage --run-id ingest-20260423T120000Z"
        echo
        echo "  -ingest-boundaries [OPTIONS]  Re-run OSM/OHM geometry completion (alias for -boundary-pass)"
        echo "  -boundary-pass [OPTIONS]      Manual repair/replay of OSM/OHM full boundary geometry"
        echo "  -generate-tiles [OPTIONS]     Generate .mbtiles from boundary places in ES"
        echo "    --source osm|ohm|both     Which PBF to process (default: both)"
        echo
        echo "  Examples:"
        echo "    $0 -boundary-pass                      # Re-run geometry completion for both OSM + OHM"
        echo "    $0 -boundary-pass --source osm         # Re-run geometry completion for OSM only"
        echo "    $0 -generate-tiles                     # Generate all tilesets"
        echo
        echo "STAGED CCODE ENRICHMENT (replaces -augment-ccodes):"
        echo "  python -m processing.submit_ccode_slurm --run-id <RUN_ID>"
        echo "      Per-namespace ccode patches built against the staged UN H3 coverage,"
        echo "      then merged into the namespace's final/places.parquet by ccode_merge."
        echo
        echo "HARD-LINK SQLITE OVERLAY (replaces ES post-index clustering):"
        echo "  python -m processing.submit_hardlinks_slurm --run-id <RUN_ID> \\"
        echo "      --pitt-user USER --pitt-host HOST --pitt-dir DIR"
        echo "      Harvest authority + LOC + contributor hard-links into a SQLite file"
        echo "      and ship-to-Pitt with atomic swap (Master Plan §2a)."
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

