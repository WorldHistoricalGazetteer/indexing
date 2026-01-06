#!/bin/bash
#SBATCH --job-name=es-staging
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=300G
#SBATCH --output=/ix1/whcdh/es/staging-logs/slurm-%j.out
#SBATCH --error=/ix1/whcdh/es/staging-logs/slurm-%j.err

# =============================================================================
# STAGING ELASTICSEARCH INSTANCE
# =============================================================================
# Runs a single staging ES instance on a Slurm compute node.
# Only ONE staging instance should run at a time - all indexing jobs share it.
#
# Start with:  source /ix1/whcdh/elastic/scripts/es.sh -staging-start
# Stop with:   source /ix1/whcdh/elastic/scripts/es.sh -staging-stop
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
ENV_FILE="/ix1/whcdh/elastic/.env"

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

# --- Find latest snapshot containing both indices ---
echo "Checking for existing snapshots..."
LATEST_SNAPSHOT=$(curl -s "$ES_URL/_snapshot/$STAGING_REPO_NAME/_all" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    snapshots = data.get('snapshots', [])
    # Find snapshots that contain both places and toponyms
    complete = [s for s in snapshots
                if s['state'] == 'SUCCESS'
                and 'places' in s['indices']
                and 'toponyms' in s['indices']]
    if complete:
        latest = max(complete, key=lambda x: x['start_time'])
        print(latest['snapshot'])
except:
    pass
" 2>/dev/null || true)

if [ -n "$LATEST_SNAPSHOT" ]; then
    # --- Restore from snapshot ---
    echo "Restoring indices from snapshot: $LATEST_SNAPSHOT"
    curl -s -X POST "$ES_URL/_snapshot/$STAGING_REPO_NAME/$LATEST_SNAPSHOT/_restore?wait_for_completion=true" \
        -H 'Content-Type: application/json' -d '{
      "indices": "places,toponyms",
      "ignore_unavailable": true,
      "include_global_state": false
    }' | python3 -m json.tool
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