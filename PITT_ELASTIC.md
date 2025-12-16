# CRC Operations Guide - WHG Elasticsearch Ingestion

Complete guide for running ingestion operations on University of Pittsburgh CRC.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Starting the Staging Instance](#starting-the-staging-instance)
3. [Running Ingestion Scripts](#running-ingestion-scripts)
4. [Creating Snapshots](#creating-snapshots)
5. [Deploying to Production](#deploying-to-production)
6. [Monitoring and Troubleshooting](#monitoring-and-troubleshooting)

---

## Prerequisites

### SSH to CRC Login Node

```bash
ssh stg135@htc.crc.pitt.edu
```

### Verify Environment

```bash
# Check .env exists
cat /ix1/whcdh/elastic/.env | head -20

# Check binaries
ls -la /ix1/whcdh/es-bin/bin/elasticsearch
ls -la /ix1/whcdh/jdk-21.0.1/bin/java
```

### Update Authority Files (if needed)

```bash
cd /ix1/whcdh/elastic
sbatch processing/refresh_authorities.slurm
```

### Update Code from GitHub

```bash
cd /ix1/whcdh/elastic
git pull origin main
```

---

## Starting the Staging Instance

All indexing operations use the staging Elasticsearch instance on a compute node.

```bash
# From login node (must use 'source' to export variables)
source /ix1/whcdh/elastic/scripts/es.sh -staging-start
```

This will:
1. Submit a Slurm job (up to 48 hours)
2. Start Elasticsearch on local NVMe (port 9201)
3. Restore the latest snapshots
4. Export `ES_NODE` and `ES_PORT` to your shell

Verify it's working:

```bash
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health?pretty"
curl -s "http://$ES_NODE:$ES_PORT/_cat/indices?v"
```

---

## Running Ingestion Scripts

### Working Directory

All commands should be run from the repository root:

```bash
cd /ix1/whcdh/elastic
```

### Step 1: Create Indices (if starting fresh)

Only needed if indices don't exist or you want to start over:

```bash
python -m processing.create_indices
```

**Warning**: This destroys existing indices!

### Step 2: Index GeoNames Places (2-3 hours)

```bash
python -m authorities.geonames_places
```

**Input**: `/ix1/whcdh/data/authorities/gn/allCountries.zip`

Monitor progress:

```bash
watch -n 30 "curl -s http://$ES_NODE:$ES_PORT/places/_count | jq ."
```

### Step 3: Index GeoNames Toponyms (4-6 hours)

```bash
python -m authorities.geonames_toponyms
```

**Input**: `/ix1/whcdh/data/authorities/gn/alternateNamesV2.zip`

### Step 4: Create Checkpoint Snapshot

After completing GeoNames:

```bash
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/geonames_$(date +%Y%m%d)?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
    "indices": "places,toponyms",
    "ignore_unavailable": true,
    "include_global_state": false
}'
```

### Step 5: Index Wikidata Places (8-12 hours)

```bash
python -m authorities.wikidata_places
```

**Input**: `/ix1/whcdh/data/authorities/wd/latest-all.json.gz`

### Step 6: Create Checkpoint Snapshot

```bash
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/wikidata_$(date +%Y%m%d)?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
    "indices": "places,toponyms",
    "ignore_unavailable": true,
    "include_global_state": false
}'
```

### Step 7: Index Remaining Authorities

```bash
# Pleiades (30-60 min)
python -m authorities.pleiades_places

# TGN (2-4 hours)
python -m authorities.tgn_places

# GB1900 (30-60 min)
python -m authorities.gb1900_places

# UN Countries (5 min)
python -m authorities.un_countries
```

### Step 8: Optional - Wikidata Geoshapes (4-8 hours)

Fetches complex geometries for Wikidata places:

```bash
python -m authorities.wikidata_geoshapes
```

### Step 9: Generate Phonetic Embeddings

```bash
python -m toponyms.generate_bilstm_embeddings --model /path/to/model.pt
```

### Step 10: Final Snapshot

```bash
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/complete_$(date +%Y%m%d)?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
    "indices": "places,toponyms",
    "ignore_unavailable": true,
    "include_global_state": false
}'
```

---

## Creating Snapshots

Snapshots must be created **explicitly** after completing each logical unit of work.

### Create a Snapshot

```bash
SNAPSHOT_NAME="checkpoint_$(date +%Y%m%d_%H%M%S)"
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/$SNAPSHOT_NAME?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
    "indices": "places,toponyms",
    "ignore_unavailable": true,
    "include_global_state": false
}'
```

### List Snapshots

```bash
curl -s "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/_all?pretty" | \
    python3 -c "import sys,json; [print(s['snapshot'], s['state'], s['start_time']) for s in json.load(sys.stdin)['snapshots']]"
```

### Delete Old Snapshots

```bash
curl -X DELETE "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/old_snapshot_name"
```

---

## Deploying to Production

After completing all indexing and creating a final snapshot:

### 1. Stop the Staging Instance

```bash
source /ix1/whcdh/elastic/scripts/es.sh -staging-stop
```

### 2. Deploy to Production (on VM)

```bash
cd /ix1/whcdh/elastic
python -m processing.deploy_to_production
```

This script will:
1. Find the latest staging snapshot
2. Restore to new timestamped indices (e.g., `places_20241216`)
3. Reconfigure index settings for production queries:
   - `refresh_interval`: `-1` → `1s` (enable near real-time search)
   - `translog.durability`: `async` → `request` (data safety)
   - `translog.flush_threshold_size`: `1gb` → `512mb` (bounded recovery)
4. Run force merge to 1 segment per shard (~30-60 minutes, but optimises query performance)
5. Atomically switch aliases (`places`, `toponyms`) to new indices
6. Optionally clean up old indices

See [INDEX_SCHEMAS.md](INDEX_SCHEMAS.md) for full details on staging vs production settings.

### 3. Verify Production

```bash
curl -s "http://localhost:9200/_cat/indices?v"
curl -s "http://localhost:9200/places/_count?pretty"
curl -s "http://localhost:9200/toponyms/_count?pretty"
```

---

## Monitoring and Troubleshooting

### Check Staging Status

```bash
source /ix1/whcdh/elastic/scripts/es.sh -staging-status
source /ix1/whcdh/elastic/scripts/es.sh -staging-health
source /ix1/whcdh/elastic/scripts/es.sh -staging-logs
```

### Check Elasticsearch Health

```bash
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health?pretty"
curl -s "http://$ES_NODE:$ES_PORT/_cat/indices?v"
```

### Document Counts by Source

```bash
for ns in gn wd pl tgn gb un; do
    count=$(curl -s "http://$ES_NODE:$ES_PORT/places/_count" \
        -H 'Content-Type: application/json' \
        -d "{\"query\": {\"prefix\": {\"place_id\": \"$ns:\"}}}" | jq .count)
    echo "$ns: $count"
done
```

### Monitor Disk Space

```bash
# Staging (ephemeral NVMe)
curl -s "http://$ES_NODE:$ES_PORT/_cat/allocation?v"

# Production (ix3 flash)
df -h /ix3/whcdh/es/data/

# Snapshots (ix1)
du -sh /ix1/whcdh/es/snapshots/staging/
```

### Script Crashes

All ingestion scripts use streaming and can be restarted safely:
- They process line-by-line from source files
- Elasticsearch handles duplicate IDs (updates existing)
- No need to delete indices and start over

### Staging Timeout

If the 48-hour limit is reached:
- Uncommitted work is lost
- The last explicit snapshot is preserved
- Start a new staging instance: `source es.sh -staging-start`
- Snapshots restore automatically
- Continue from where you left off

### Out of Memory

If ingestion scripts are killed:

1. Reduce batch size in `processing/settings.py`:
   ```python
   BATCH_SIZE = 2500  # Reduce from 5000
   ```

2. Or request more memory in `es_staging.sbatch`:
   ```bash
   #SBATCH --mem=32G
   ```

---

## Expected Timeline

| Step | Script | Runtime | Documents (approx) |
|------|--------|---------|-------------------|
| Create indices | `create_indices` | < 1 min | - |
| GeoNames places | `geonames_places` | 2-3 hrs | ~12 million |
| GeoNames toponyms | `geonames_toponyms` | 4-6 hrs | ~17 million |
| Wikidata places | `wikidata_places` | 8-12 hrs | ~10-15 million |
| Pleiades | `pleiades_places` | 30-60 min | ~37,000 |
| TGN | `tgn_places` | 2-4 hrs | ~2.5 million |
| GB1900 | `gb1900_places` | 30-60 min | ~1.5 million |
| UN Countries | `un_countries` | 5 min | ~200 |
| Wikidata geoshapes | `wikidata_geoshapes` | 4-8 hrs | ~130,000 updates |
| Embeddings | `generate_bilstm_embeddings` | TBD | - |
| Deploy to production | `deploy_to_production` | 30-60 min | - |

**Total**: ~20-30 hours of compute time, spread across multiple staging sessions.

---

## Expected Final Results

### Document Counts

```bash
curl -s "http://localhost:9200/places/_count?pretty"
# Expected: ~25-30 million

curl -s "http://localhost:9200/toponyms/_count?pretty"
# Expected: ~80 million unique
```

### Storage

| Location | Size |
|----------|------|
| Production indices (ix3) | 180-270 GB |
| Staging snapshots (ix1) | ~100 GB |

---

## Quick Reference

```bash
# Start staging
source /ix1/whcdh/elastic/scripts/es.sh -staging-start

# Check health
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health?pretty"

# Count documents
curl -s "http://$ES_NODE:$ES_PORT/places/_count?pretty"
curl -s "http://$ES_NODE:$ES_PORT/toponyms/_count?pretty"

# Create snapshot
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/checkpoint_$(date +%Y%m%d)?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{"indices": "places,toponyms"}'

# List snapshots
curl -s "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/_all?pretty"

# Stop staging
source /ix1/whcdh/elastic/scripts/es.sh -staging-stop

# Deploy to production (on VM)
python -m processing.deploy_to_production
```

---

## Related Documentation

- [README.md](README.md) — Overview and service management
- [ES_STAGING.md](ES_STAGING.md) — Staging instance details
- [INDEX_SCHEMAS.md](INDEX_SCHEMAS.md) — Index mappings and settings