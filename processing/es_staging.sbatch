#!/bin/bash
#SBATCH --job-name=es-staging
#SBATCH --time=48:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=20G
#SBATCH --output=/ix1/whcdh/es-staging/logs/slurm-%j.out
#SBATCH --error=/ix1/whcdh/es-staging/logs/slurm-%j.err

# Elasticsearch + Java installations
ES_HOME=/ix1/whcdh/es-bin
ES_BIN_PATH=$ES_HOME/bin/elasticsearch
JAVA_HOME=/ix1/whcdh/jdk-21.0.1
export PATH="$JAVA_HOME/bin:$PATH"

# Per-job isolated staging root on local NVMe scratch
STAGING_ROOT="${SLURM_SCRATCH}/es-staging"
DATA_DIR="$STAGING_ROOT/data"
LOG_DIR="$STAGING_ROOT/logs"
CONF_DIR="$STAGING_ROOT/config"

# Shared snapshot repository (from production)
SHARED_REPO_DIR="/ix1/whcdh/es/repo/staging"
REPO_NAME="staging_repo"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$CONF_DIR"

# Copy required config files from ES installation
cp -r $ES_HOME/config/* "$CONF_DIR/"

# ES needs ES_PATH_CONF even if empty or minimal
export ES_PATH_CONF="$CONF_DIR"

# Unique port (safe even with many jobs)
ES_PORT=$((9200 + SLURM_JOB_ID % 5000))

# Write info for other Slurm jobs to consume
INFO_FILE="/ix1/whcdh/esinfo/es-${SLURM_JOB_ID}.env"
mkdir -p /ix1/whcdh/esinfo

cat > "$INFO_FILE" <<EOF
ES_NODE=$SLURMD_NODENAME
ES_PORT=$ES_PORT
ES_DATA=$DATA_DIR
ES_ROOT=$STAGING_ROOT
EOF

echo "Elasticsearch staging instance:"
echo "  Node: $SLURMD_NODENAME"
echo "  Port: $ES_PORT"
echo "  Root: $STAGING_ROOT"
echo

# Function to create snapshots before shutdown
create_final_snapshots() {
    echo
    echo "=========================================="
    echo "Creating final snapshots before shutdown..."
    echo "=========================================="

    ES_URL="http://localhost:$ES_PORT"
    TIMESTAMP=$(date +%Y%m%d%H%M%S)

    # Check if ES is still responsive
    if ! curl -s "$ES_URL/_cluster/health" > /dev/null 2>&1; then
        echo "WARNING: Elasticsearch not responsive, skipping snapshots"
        return
    fi

    # Snapshot places index
    PLACES_SNAPSHOT="places_staging_${TIMESTAMP}"
    echo "Creating snapshot: $PLACES_SNAPSHOT"
    PLACES_RESPONSE=$(curl -s -X PUT "$ES_URL/_snapshot/$REPO_NAME/$PLACES_SNAPSHOT?wait_for_completion=true" \
        -H 'Content-Type: application/json' -d '{
      "indices": "places",
      "ignore_unavailable": true,
      "include_global_state": false
    }')

    PLACES_STATE=$(echo "$PLACES_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('snapshot', {}).get('state', 'UNKNOWN'))")
    echo "Places snapshot state: $PLACES_STATE"

    # Snapshot toponyms index
    TOPONYMS_SNAPSHOT="toponyms_staging_${TIMESTAMP}"
    echo "Creating snapshot: $TOPONYMS_SNAPSHOT"
    TOPONYMS_RESPONSE=$(curl -s -X PUT "$ES_URL/_snapshot/$REPO_NAME/$TOPONYMS_SNAPSHOT?wait_for_completion=true" \
        -H 'Content-Type: application/json' -d '{
      "indices": "toponyms",
      "ignore_unavailable": true,
      "include_global_state": false
    }')

    TOPONYMS_STATE=$(echo "$TOPONYMS_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('snapshot', {}).get('state', 'UNKNOWN'))")
    echo "Toponyms snapshot state: $TOPONYMS_STATE"

    echo
    echo "Final snapshots completed:"
    echo "  - $PLACES_SNAPSHOT: $PLACES_STATE"
    echo "  - $TOPONYMS_SNAPSHOT: $TOPONYMS_STATE"
    echo "=========================================="
}

# Cleanup on termination (job end, cancel, preemption)
cleanup() {
    echo
    echo "=========================================="
    echo "Shutdown initiated..."
    echo "=========================================="

    # Create final snapshots (this waits for completion)
    create_final_snapshots

    # Give ES time to finalize repository metadata
    echo "Waiting for Elasticsearch to finalize snapshot metadata (15 seconds)..."
    sleep 15

    # Gracefully stop ES to ensure all data is flushed
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
            echo "WARNING: Force killing Elasticsearch (data may not be fully flushed)"
            kill -9 "$ES_PID" 2>/dev/null
            sleep 5
        fi
    fi

    # Additional sync time for NFS
    echo "Syncing filesystem..."
    sync
    sleep 5

    echo "Cleaning up staging directory: $STAGING_ROOT"
    rm -rf "$STAGING_ROOT" 2>/dev/null || true
    rm -f "$INFO_FILE"
    echo "Cleanup complete."
}
trap cleanup EXIT

# Set JVM heap size (use about half of allocated memory for heap)
# Leave the rest for off-heap structures, OS cache, etc.
export ES_JAVA_OPTS="-Xms8g -Xmx8g"

# Start Elasticsearch in background
$ES_BIN_PATH \
    -E discovery.type=single-node \
    -E cluster.name="es-staging-${SLURM_JOB_ID}" \
    -E node.name="es-staging-node-${SLURM_JOB_ID}" \
    -E network.host=127.0.0.1 \
    -E http.port="$ES_PORT" \
    -E path.data="$DATA_DIR" \
    -E path.logs="$LOG_DIR" \
    -E path.repo="$SHARED_REPO_DIR" \
    -E xpack.security.enabled=false \
    > "$LOG_DIR/elasticsearch.log" 2>&1 &

ES_PID=$!
echo "Elasticsearch PID: $ES_PID"

# Wait for ES to be ready
echo "Waiting for Elasticsearch to start..."
ES_URL="http://localhost:$ES_PORT"
for i in {1..60}; do
    if curl -s "$ES_URL/_cluster/health" > /dev/null 2>&1; then
        echo "Elasticsearch is ready!"
        break
    fi
    if ! kill -0 $ES_PID 2>/dev/null; then
        echo "ERROR: Elasticsearch died during startup"
        exit 1
    fi
    sleep 2
done

# Register snapshot repository
echo "Registering snapshot repository '$REPO_NAME'..."
curl -s -X PUT "$ES_URL/_snapshot/$REPO_NAME" -H 'Content-Type: application/json' -d "{
  \"type\": \"fs\",
  \"settings\": {
    \"location\": \"$SHARED_REPO_DIR\"
  }
}" | python3 -m json.tool
echo

# Find latest snapshots for each index
echo "Finding latest snapshots..."
SNAPSHOTS=$(curl -s "$ES_URL/_snapshot/$REPO_NAME/_all")

# Extract latest places snapshot
LATEST_PLACES=$(echo "$SNAPSHOTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
places_snaps = [s for s in data.get('snapshots', []) if 'places_staging' in s['snapshot']]
if places_snaps:
    latest = max(places_snaps, key=lambda x: x['start_time'])
    print(latest['snapshot'])
")

# Extract latest toponyms snapshot
LATEST_TOPONYMS=$(echo "$SNAPSHOTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
toponyms_snaps = [s for s in data.get('snapshots', []) if 'toponyms_staging' in s['snapshot']]
if toponyms_snaps:
    latest = max(toponyms_snaps, key=lambda x: x['start_time'])
    print(latest['snapshot'])
")

# Restore places index
if [ -n "$LATEST_PLACES" ]; then
    echo "Restoring places index from snapshot: $LATEST_PLACES"
    curl -s -X POST "$ES_URL/_snapshot/$REPO_NAME/$LATEST_PLACES/_restore" \
        -H 'Content-Type: application/json' -d '{
      "indices": "places",
      "ignore_unavailable": true,
      "include_global_state": false
    }' | python3 -m json.tool
    echo
else
    echo "WARNING: No places snapshots found"
fi

# Restore toponyms index
if [ -n "$LATEST_TOPONYMS" ]; then
    echo "Restoring toponyms index from snapshot: $LATEST_TOPONYMS"
    curl -s -X POST "$ES_URL/_snapshot/$REPO_NAME/$LATEST_TOPONYMS/_restore" \
        -H 'Content-Type: application/json' -d '{
      "indices": "toponyms",
      "ignore_unavailable": true,
      "include_global_state": false
    }' | python3 -m json.tool
    echo
else
    echo "WARNING: No toponyms snapshots found"
fi

echo
echo "=========================================="
echo "Staging ES is ready at: http://localhost:$ES_PORT"
echo "=========================================="
echo "Monitor restore progress with:"
echo "  ssh $SLURMD_NODENAME \"curl -s http://localhost:$ES_PORT/_recovery?pretty\""
echo
echo "To create snapshots before shutdown, use:"
echo "  scancel $SLURM_JOB_ID"
echo "  (snapshots will be created automatically)"
echo
echo "Keeping ES running until job ends..."

# Wait for ES process (keeps job alive)
wait $ES_PID