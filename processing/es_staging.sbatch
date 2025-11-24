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

# Per-job isolated staging root
STAGING_ROOT="/ix1/whcdh/es-staging/${SLURM_JOB_ID}"
DATA_DIR="$STAGING_ROOT/data"
LOG_DIR="$STAGING_ROOT/logs"
CONF_DIR="$STAGING_ROOT/config"
REPO_DIR="$STAGING_ROOT/repo"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$CONF_DIR" "$REPO_DIR"

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
echo "Local access example:"
echo "  ssh -L 9200:localhost:$ES_PORT $USER@$SLURMD_NODENAME"
echo "  Then open: http://localhost:9200"
echo

# Cleanup on termination (job end, cancel, preemption)
cleanup() {
    echo "Cleaning up staging directory: $STAGING_ROOT"
    rm -rf "$STAGING_ROOT"
    rm -f "$INFO_FILE"
}
trap cleanup EXIT

# Set JVM heap size (use about half of allocated memory for heap)
# Leave the rest for off-heap structures, OS cache, etc.
export ES_JAVA_OPTS="-Xms8g -Xmx8g"

# Start Elasticsearch
exec $ES_BIN_PATH \
    -E discovery.type=single-node \
    -E cluster.name="es-staging-${SLURM_JOB_ID}" \
    -E node.name="es-staging-node-${SLURM_JOB_ID}" \
    -E network.host=127.0.0.1 \
    -E http.port="$ES_PORT" \
    -E path.data="$DATA_DIR" \
    -E path.logs="$LOG_DIR" \
    -E path.repo="$REPO_DIR" \
    -E xpack.security.enabled=false
