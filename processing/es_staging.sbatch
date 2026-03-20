#!/bin/bash
#SBATCH --job-name=es-staging
#SBATCH --partition=smp
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G
#SBATCH --output=/ix1/ishi/es/staging-logs/slurm-%j.out
#SBATCH --error=/ix1/ishi/es/staging-logs/slurm-%j.err

# =============================================================================
# STAGING ELASTICSEARCH INSTANCE
# =============================================================================
# Runs a single staging ES instance on a Slurm compute node.
# Only ONE staging instance should run at a time - all indexing jobs share it.
#
# Start with:  source /ix1/ishi/elastic/scripts/es.sh -staging-start
# Stop with:   source /ix1/ishi/elastic/scripts/es.sh -staging-stop
#
# On startup:
#   - Restores latest snapshots if any exist
#   - Otherwise creates empty indices using create_indices.py
#
# Snapshots must be created EXPLICITLY after completing work.
# No automatic snapshots are created on shutdown.
# =============================================================================

set -e

# --- Load environment configuration ---
# Use absolute path since SCRIPT_DIR doesn't work reliably in Slurm
ENV_FILE="/ix1/ishi/elastic/.env"

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "ERROR: .env file not found at $ENV_FILE"
    exit 1
fi

export PATH="$JAVA_HOME/bin:$PATH"

# --- Staging-specific settings ---
# Data on fast local NVMe scratch (ephemeral)
STAGING_ROOT="${SLURM_SCRATCH}/es-staging"
STAGING_DATA_DIR="${STAGING_ROOT}/data"
STAGING_LOG_DIR="${STAGING_ROOT}/logs"
STAGING_CONFIG_DIR="${STAGING_ROOT}/config"

# Create directories
mkdir -p "$STAGING_DATA_DIR" "$STAGING_LOG_DIR" "$STAGING_CONFIG_DIR"
mkdir -p "$(dirname "$STAGING_INFO_FILE")"
mkdir -p "$STAGING_SNAPSHOT_DIR"

# Copy ES config files
cp -r "$ES_HOME/config/"* "$STAGING_CONFIG_DIR/"
export ES_PATH_CONF="$STAGING_CONFIG_DIR"

# ES 9.0 jvm.options contains a relative 'logs/gc.log' path which fails when
# the working directory doesn't have a writable logs/ subdir.
# Patch the copy to use our absolute staging log dir.
if [ -f "$STAGING_CONFIG_DIR/jvm.options" ]; then
    sed -i "s|file=logs/gc\.log|file=${STAGING_LOG_DIR}/gc.log|g" \
        "$STAGING_CONFIG_DIR/jvm.options"
    echo "Patched jvm.options: gc.log -> ${STAGING_LOG_DIR}/gc.log"
fi

# --- Write connection info for other jobs/scripts ---
cat > "$STAGING_INFO_FILE" <<EOF
ES_NODE=$SLURMD_NODENAME
ES_PORT=$STAGING_ES_PORT
ES_URL=http://$SLURMD_NODENAME:$STAGING_ES_PORT
ES_DATA=$STAGING_DATA_DIR
ES_ROOT=$STAGING_ROOT
SLURM_JOB_ID=$SLURM_JOB_ID
EOF

echo "=========================================="
echo "STAGING ELASTICSEARCH INSTANCE"
echo "=========================================="
echo "  Node: $SLURMD_NODENAME"
echo "  Port: $STAGING_ES_PORT"
echo "  Data: $STAGING_DATA_DIR (ephemeral NVMe)"
echo "  Snapshots: $STAGING_SNAPSHOT_DIR (persistent)"
echo "  Info: $STAGING_INFO_FILE"
echo "  Job:  $SLURM_JOB_ID"
echo

# --- Cleanup on termination ---
cleanup() {
    echo
    echo "=========================================="
    echo "SHUTDOWN INITIATED"
    echo "=========================================="
    echo
    echo "NOTE: No automatic snapshots are created."
    echo "If you have unsaved work, cancel and create snapshots manually."
    echo

    # Gracefully stop ES
    if [ -n "$ES_PID" ] && kill -0 "$ES_PID" 2>/dev/null; then
        echo "Sending shutdown signal to Elasticsearch (PID: $ES_PID)..."
        kill -TERM "$ES_PID"

        # Wait for graceful shutdown (up to 60 seconds)
        for i in {1..60}; do
            if ! kill -0 "$ES_PID" 2>/dev/null; then
                echo "Elasticsearch stopped gracefully."
                break
            fi
            echo -n "."
            sleep 1
        done
        echo

        # Force kill if still running
        if kill -0 "$ES_PID" 2>/dev/null; then
            echo "WARNING: Force killing Elasticsearch"
            kill -9 "$ES_PID" 2>/dev/null
            sleep 5
        fi
    fi

    # Sync filesystem
    echo "Syncing filesystem..."
    sync
    sleep 2

    # Clean up ephemeral data
    echo "Cleaning up staging directory: $STAGING_ROOT"
    rm -rf "$STAGING_ROOT" 2>/dev/null || true
    rm -f "$STAGING_INFO_FILE"
    echo "Cleanup complete."
}
trap cleanup EXIT

# --- JVM settings for bulk indexing ---
export ES_JAVA_OPTS="-Xms31g -Xmx31g"
export ES_TMPDIR="/tmp"

# --- Start Elasticsearch ---
echo "Starting Elasticsearch..."

"$ES_HOME/bin/elasticsearch" \
    -E discovery.type=single-node \
    -E cluster.name="$STAGING_CLUSTER_NAME" \
    -E node.name="$STAGING_NODE_NAME" \
    -E network.host=0.0.0.0 \
    -E http.port="$STAGING_ES_PORT" \
    -E path.data="$STAGING_DATA_DIR" \
    -E path.logs="$STAGING_LOG_DIR" \
    -E path.repo="$SNAPSHOT_DIR" \
    -E xpack.security.enabled=false \
    -E indices.memory.index_buffer_size=30% \
    > "$STAGING_LOG_DIR/elasticsearch.log" 2>&1 &

ES_PID=$!
echo "Elasticsearch PID: $ES_PID"

# --- Wait for ES to be ready ---
echo "Waiting for Elasticsearch to start..."
ES_URL="http://localhost:$STAGING_ES_PORT"

for i in {1..60}; do
    if curl -s "$ES_URL/_cluster/health" > /dev/null 2>&1; then
        echo "Elasticsearch is ready!"
        break
    fi
    if ! kill -0 $ES_PID 2>/dev/null; then
        echo "ERROR: Elasticsearch died during startup"
        cat "$STAGING_LOG_DIR/elasticsearch.log"
        exit 1
    fi
    sleep 2
done

# Verify ES is responding
if ! curl -s "$ES_URL/_cluster/health" > /dev/null 2>&1; then
    echo "ERROR: Elasticsearch failed to start within timeout"
    cat "$STAGING_LOG_DIR/elasticsearch.log"
    exit 1
fi

# --- Register snapshot repository ---
echo
echo "Registering snapshot repository '$STAGING_REPO_NAME'..."
curl -s -X PUT "$ES_URL/_snapshot/$STAGING_REPO_NAME" \
    -H 'Content-Type: application/json' -d "{
  \"type\": \"fs\",
  \"settings\": {
    \"location\": \"$STAGING_SNAPSHOT_DIR\"
  }
}" | python3 -m json.tool
echo

# Register cluster exchange repo (for snapshot handoff with production)
if [ -n "$CLUSTER_EXCHANGE_DIR" ]; then
    mkdir -p "$CLUSTER_EXCHANGE_DIR"
    echo "Registering snapshot repository '$CLUSTER_EXCHANGE_REPO'..."
    curl -s -X PUT "$ES_URL/_snapshot/$CLUSTER_EXCHANGE_REPO" \
        -H 'Content-Type: application/json' -d "{
      \"type\": \"fs\",
      \"settings\": {
        \"location\": \"$CLUSTER_EXCHANGE_DIR\"
      }
    }" | python3 -m json.tool
    echo
fi

# --- Find latest snapshot containing required indices ---
echo "Checking for existing snapshots..."

# NOTE: 'places' and 'toponyms' are aliases pointing to versioned concrete
# indices (e.g. places_20250320, toponyms_20250320).  Snapshots store concrete
# index names, so we must match by prefix rather than exact name.

# Determine which index prefixes to look for and restore
if [ "${RESTORE_PLACES_ONLY:-0}" = "1" ]; then
    echo "Mode: places-only (toponyms will be rebuilt separately)"
    REQUIRED_PREFIX="places"
    RESTORE_PREFIXES="places"
else
    REQUIRED_PREFIX="places"  # At minimum need places
    RESTORE_PREFIXES="places toponyms"
fi

# Find the latest successful snapshot whose indices include a places* entry,
# and output its name plus the full list of concrete indices it contains.
SNAPSHOT_INFO=$(curl -s "$ES_URL/_snapshot/$STAGING_REPO_NAME/_all" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    snapshots = data.get('snapshots', [])
    prefix = '${REQUIRED_PREFIX}'
    valid = [s for s in snapshots
             if s['state'] == 'SUCCESS'
             and any(idx == prefix or idx.startswith(prefix + '_')
                     for idx in s['indices'])]
    if valid:
        latest = max(valid, key=lambda x: x['start_time'])
        # Line 1: snapshot name  Line 2: comma-separated concrete indices
        print(latest['snapshot'])
        print(','.join(latest['indices']))
except:
    pass
" 2>/dev/null || true)

LATEST_SNAPSHOT=$(echo "$SNAPSHOT_INFO" | head -1)
ALL_SNAPSHOT_INDICES=$(echo "$SNAPSHOT_INFO" | sed -n '2p')

if [ -n "$LATEST_SNAPSHOT" ]; then
    # Filter snapshot indices to those matching our desired prefixes
    RESTORE_INDICES=""
    for PREFIX in $RESTORE_PREFIXES; do
        for IDX in $(echo "$ALL_SNAPSHOT_INDICES" | tr ',' ' '); do
            if [ "$IDX" = "$PREFIX" ] || echo "$IDX" | grep -qE "^${PREFIX}_"; then
                RESTORE_INDICES="${RESTORE_INDICES:+$RESTORE_INDICES,}$IDX"
            fi
        done
    done

    # --- Restore from snapshot ---
    echo "Restoring indices from snapshot: $LATEST_SNAPSHOT"
    echo "  Concrete indices: $RESTORE_INDICES"
    # Start restore WITHOUT wait_for_completion so ES remains responsive
    curl -s -X POST "$ES_URL/_snapshot/$STAGING_REPO_NAME/$LATEST_SNAPSHOT/_restore" \
        -H 'Content-Type: application/json' -d "{
      \"indices\": \"$RESTORE_INDICES\",
      \"ignore_unavailable\": true,
      \"include_global_state\": false
    }" | python3 -m json.tool
    echo

    # Wait for restore to complete by checking recovery status
    echo "Waiting for snapshot restore to complete..."
    echo "  (This may take 10-30 minutes for large indices)"
    while true; do
        RECOVERING=$(curl -s "$ES_URL/_cat/recovery?active_only=true" 2>/dev/null | wc -l)
        if [ "$RECOVERING" -eq 0 ]; then
            echo "Snapshot restore complete!"
            break
        fi
        echo -n "  Shards still recovering: $RECOVERING"
        # Show progress
        PROGRESS=$(curl -s "$ES_URL/_cat/recovery?v&active_only=true" 2>/dev/null | head -5 || true)
        echo "$PROGRESS" | tail -1 | awk '{print " - " $NF}'
        sleep 10
    done

    # Staging is single-node so replica shards can never be assigned.
    # Set replicas to 0 to reach green health and avoid wasted resources.
    echo "Setting number_of_replicas=0 on restored indices ..."
    for IDX in $(echo "$RESTORE_INDICES" | tr ',' ' '); do
        curl -s -X PUT "$ES_URL/${IDX}/_settings" \
            -H 'Content-Type: application/json' \
            -d '{"index":{"number_of_replicas":0}}' >/dev/null 2>&1 || true
    done

    # --- Ensure aliases exist for versioned indices ---
    # Snapshots restore aliases alongside indices, but verify they're present
    # and create them if not (e.g. snapshot predates alias creation).
    echo "Verifying aliases..."
    for BASE_NAME in places toponyms; do
        # Skip if this base wasn't part of the restore
        echo "$RESTORE_INDICES" | tr ',' '\n' | grep -qE "^${BASE_NAME}(_|$)" || continue

        CONCRETE=$(echo "$RESTORE_INDICES" | tr ',' '\n' | grep -E "^${BASE_NAME}(_|$)" | head -1)

        if [ "$CONCRETE" = "$BASE_NAME" ]; then
            echo "  $BASE_NAME is a concrete index (no alias needed)"
        else
            # Check whether the alias already exists (may have been restored with the snapshot)
            ALIAS_EXISTS=$(curl -s -o /dev/null -w "%{http_code}" "$ES_URL/_alias/$BASE_NAME")
            if [ "$ALIAS_EXISTS" = "200" ]; then
                echo "  $BASE_NAME alias already exists (restored with snapshot)"
            else
                echo "  Creating alias: $BASE_NAME -> $CONCRETE"
                curl -s -X POST "$ES_URL/_aliases" \
                    -H 'Content-Type: application/json' -d "{
                  \"actions\": [{\"add\": {\"index\": \"$CONCRETE\", \"alias\": \"$BASE_NAME\"}}]
                }" | python3 -m json.tool
            fi
        fi
    done
    echo
else
    # --- Create empty indices ---
    echo "No complete snapshots found - creating empty indices..."
    cd "$REPO_DIR"

    # Activate conda environment if available
    if [ -f "$IX1_BASE/miniconda/etc/profile.d/conda.sh" ]; then
        source "$IX1_BASE/miniconda/etc/profile.d/conda.sh"
        conda activate whg 2>/dev/null || true
    fi

    # Set ES_HOST for the Python script
    export ES_HOST="$ES_URL"

    python -m processing.create_indices
    echo
fi

# --- Show index status ---
echo
echo "Index status:"
curl -s "$ES_URL/_cat/indices?v" || true
echo

echo "=========================================="
echo "STAGING ES READY"
echo "=========================================="
echo
echo "URL: http://$SLURMD_NODENAME:$STAGING_ES_PORT"
echo
echo "From other jobs/scripts, source: $STAGING_INFO_FILE"
echo
echo "IMPORTANT: Create snapshots explicitly after completing work."
echo "No automatic snapshots are created on shutdown."
echo
echo "To create a snapshot:"
echo "  curl -X PUT \"$ES_URL/_snapshot/$STAGING_REPO_NAME/checkpoint_\$(date +%Y%m%d_%H%M%S)?wait_for_completion=true\" \\"
echo "    -H 'Content-Type: application/json' -d '{\"indices\": \"places,toponyms\"}'"
echo
echo "Keeping ES running until job ends or is cancelled..."

# --- Wait for ES process ---
wait $ES_PID