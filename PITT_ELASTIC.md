# Running Elasticsearch 8.11.1 on CRC HTC with Singularity

This guide documents the setup for running Elasticsearch in a Singularity container on the University of Pittsburgh CRC (Center for Research Computing) HTC cluster.

## Prerequisites

- Access to CRC HTC cluster (`stg135@htc.crc.pitt.edu`)
- Account allocation: `whcdh`
- Shared storage at `/ix1/whcdh/data/`

## Initial Setup (One-time)

### 1. Download the Elasticsearch Singularity Image

```bash
# Pull the Elasticsearch 8.11.1 Docker image and convert to SIF
singularity pull /ix1/whcdh/data/elasticsearch-8.11.1.sif \
  docker://docker.elastic.co/elasticsearch/elasticsearch:8.11.1
```

### 2. Create Directory Structure

```bash
# Create directories for Elasticsearch data, logs, config, and snapshots
mkdir -p /ix1/whcdh/data/es-test/{data,logs,config,repo}
chmod -R 770 /ix1/whcdh/data/es-test
```

### 3. Extract and Configure Elasticsearch Config Files

```bash
# Extract default config from container
singularity exec /ix1/whcdh/data/elasticsearch-8.11.1.sif \
  sh -c 'cp -r /usr/share/elasticsearch/config /tmp/es-config && tar -czf - -C /tmp es-config' | \
  tar -xzf - -C /ix1/whcdh/data/es-test/

# Move config files to correct location
mv /ix1/whcdh/data/es-test/config/es-config/* /ix1/whcdh/data/es-test/config/
rmdir /ix1/whcdh/data/es-test/config/es-config

# Fix GC log path (important for Singularity read-only filesystem)
sed -i 's|logs/gc.log|/ix1/whcdh/data/es-test/logs/gc.log|g' \
  /ix1/whcdh/data/es-test/config/jvm.options
```

Verify the config files are in place:
```bash
ls -la /ix1/whcdh/data/es-test/config/
# Should show: elasticsearch.yml, jvm.options, log4j2.properties, etc.
```

## Running Elasticsearch

### 1. Connect to CRC and Request Interactive Job

```bash
# SSH to CRC HTC cluster
ssh stg135@htc.crc.pitt.edu

# Request an interactive session with adequate resources
crc-interactive -s -c 2 -b 8 -t 2:00:00 -a whcdh
```

This requests:
- `-s`: SMP cluster
- `-c 2`: 2 CPU cores
- `-b 8`: 8 GB RAM
- `-t 2:00:00`: 2 hour time limit
- `-a whcdh`: Account allocation

### 2. Load Singularity Module

```bash
module load singularity/3.9.6
```

### 3. Start tmux (Recommended)

```bash
# Start tmux for easy management
tmux

# Tmux quick reference:
# Ctrl+b "  - split window horizontally
# Ctrl+b %  - split window vertically
# Ctrl+b arrow keys - switch between panes
# Ctrl+b d  - detach (ES keeps running)
# tmux attach - reattach later
```

### 4. Run Elasticsearch

```bash
singularity exec \
  --bind /ix1/whcdh/es/config:/usr/share/elasticsearch/config \
  /ix1/whcdh/data/elasticsearch-8.11.1.sif \
  /bin/bash -c "ES_JAVA_OPTS='-Xms2g -Xmx2g' \
  elasticsearch \
    -E path.data=/ix1/whcdh/es/data \
    -E path.logs=/ix1/whcdh/es/logs \
    -E path.repo=/ix1/whcdh/es/repo \
    -E discovery.type=single-node \
    -E xpack.security.enabled=false \
    -E network.host=0.0.0.0"
```

**Key configuration options:**
- `--bind`: Mounts writable config directory over read-only container path
- `ES_JAVA_OPTS='-Xms2g -Xmx2g'`: Sets heap size to 2GB min/max
- `path.data`: Where indices are stored
- `path.logs`: Where logs are written
- `path.repo`: Where snapshots can be stored
- `discovery.type=single-node`: Run as single node (not a cluster)
- `xpack.security.enabled=false`: Disable authentication for testing
- `network.host=0.0.0.0`: Bind to all network interfaces

### 5. Test Elasticsearch (in new tmux pane)

Press `Ctrl+b "` to split the tmux window, then:

```bash
# Check if ES is running
curl http://localhost:9200/

# Should return JSON with cluster info like:
# {
#   "name" : "...",
#   "cluster_name" : "...",
#   "version" : {
#     "number" : "8.11.1",
#     ...
#   }
# }

# Check cluster health
curl http://localhost:9200/_cluster/health?pretty

# List indices
curl http://localhost:9200/_cat/indices?v
```

## Common Operations

### Create an Index

```bash
curl -X PUT http://localhost:9200/my_index -H 'Content-Type: application/json' -d '{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  }
}'
```

### Index a Document

```bash
curl -X POST http://localhost:9200/my_index/_doc -H 'Content-Type: application/json' -d '{
  "title": "Test Document",
  "content": "This is a test"
}'
```

### Search

```bash
curl http://localhost:9200/my_index/_search?pretty
```

### Stop Elasticsearch

Press `Ctrl+C` in the pane where Elasticsearch is running.

## Troubleshooting

### Issue: "Read-only file system" errors

**Solution:** Ensure you're using the `--bind` mount for the config directory and that the GC log path has been updated in `jvm.options`.

### Issue: "Missing logging config file"

**Solution:** Verify config files were properly extracted:
```bash
ls -la /ix1/whcdh/data/es-test/config/
```

Should see `log4j2.properties`, `elasticsearch.yml`, `jvm.options`, etc.

### Issue: Can't access from another terminal

**Solution:** Use tmux to split panes within the same interactive job session, or use:
```bash
srun --jobid=<your_job_id> --overlap --pty /bin/bash
```

### Issue: Out of memory

**Solution:** Request more RAM in your interactive session (`-b 16` for 16GB) and adjust ES heap size:
```bash
ES_JAVA_OPTS='-Xms4g -Xmx4g'
```

### Check Logs

```bash
tail -f /ix1/whcdh/data/es-test/logs/*.log
```

## File Locations Summary

- **Singularity Image:** `/ix1/whcdh/data/elasticsearch-8.11.1.sif`
- **Data Directory:** `/ix1/whcdh/data/es-test/data/`
- **Log Directory:** `/ix1/whcdh/data/es-test/logs/`
- **Config Directory:** `/ix1/whcdh/data/es-test/config/`
- **Snapshot Repository:** `/ix1/whcdh/data/es-test/repo/`

## Notes

- The setup uses security disabled (`xpack.security.enabled=false`) for testing. For production, enable security and configure authentication.
- Data persists in `/ix1/whcdh/data/es-test/data/` between runs.
- Each interactive session has a time limit (2 hours in the example). Adjust `-t` parameter as needed.
- Elasticsearch runs on default port 9200 within the compute node.

## Version Information

- **Elasticsearch:** 8.11.1
- **Singularity:** 3.9.6
- **Cluster:** CRC HTC (University of Pittsburgh)
- **Date:** November 2025
