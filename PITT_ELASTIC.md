# CRC Operations Guide - WHG Elasticsearch Ingestion

Complete guide for running Elasticsearch and ingesting gazetteer data on University of Pittsburgh CRC HTC cluster.

## Table of Contents

1. [Initial Login and Setup](#initial-login-and-setup)
2. [Starting Elasticsearch](#starting-elasticsearch)
3. [Updating Code from GitHub](#updating-code-from-github)
4. [Running Ingestion Scripts](#running-ingestion-scripts)
5. [Monitoring and Troubleshooting](#monitoring-and-troubleshooting)

---

## Initial Login and Setup

### 1. SSH to CRC

```bash
ssh stg135@htc.crc.pitt.edu
```

### Update Authority Files

The slurm script is set to update all authorities by default. It can be modified to update specific ones if needed.

```bash
cd /ix1/whcdh/elastic
sbatch processing/refresh_authorities.slurm
```

### 2. Request Interactive Session

For ingestion (12 hours maximum with crc-interactive):

```bash
crc-interactive -s -c 4 -b 16 -t 12:00:00 -a whcdh
```

**Parameters:**
- `-s`: SMP cluster
- `-c 4`: 4 CPU cores
- `-b 16`: 16 GB RAM
- `-t 48:00:00`: 48 hour time limit
- `-a whcdh`: Account allocation

**Alternative (salloc) for more than 12 hours:**
```bash
salloc -M htc -A whcdh --qos=normal -c 4 --mem=16G -t 2-00:00:00
ssh htc-nXXX  # Replace XXX with your allocated node number
```

### 3. Load Required Modules

```bash
module load singularity/3.9.6
```

### 4. Start tmux Session

```bash
tmux new -s whg-ingest

# Tmux commands:
# Ctrl+b "  - split horizontally
# Ctrl+b %  - split vertically
# Ctrl+b arrow - switch panes
# Ctrl+b d  - detach
# tmux attach -t whg-ingest  - reattach
```

---

## Starting Elasticsearch

### In tmux pane 1 (Elasticsearch server)

```bash
singularity exec \
    --bind /ix1/whcdh/es/config:/usr/share/elasticsearch/config \
    --bind /ix1/whcdh/es/logs:/usr/share/elasticsearch/logs \
    /ix1/whcdh/data/elasticsearch-8.11.1.sif \
    /bin/bash -c "ES_JAVA_OPTS='-Xms4g -Xmx4g' \
    elasticsearch \
      -E path.data=/ix1/whcdh/es/data \
      -E path.logs=/ix1/whcdh/es/logs \
      -E path.repo=/ix1/whcdh/es/repo \
      -E discovery.type=single-node \
      -E xpack.security.enabled=false \
      -E network.host=0.0.0.0"
```

**Wait 30 seconds**, then verify in new pane:

```bash
# New window: Ctrl+b : new-window
curl http://localhost:9200/
curl http://localhost:9200/_cluster/health?pretty
```

Should see cluster health status: "green" or "yellow"

---

## Updating Code from GitHub

### Navigate to Project Directory

```bash
cd /ix1/whcdh/elastic
```

### Pull Latest Changes

```bash
git pull origin main
```

### Verify File Structure

```bash
ls -l
# Should see:
# authorities/
# schemas/
# README.md
# etc.

ls authorities/
# Should see:
# create_indices.py
# geonames-places.py
# geonames-toponyms.py
# wikidata-places.py
# wikidata-geoshapes.py
# helpers.py
# settings.py
```

### Check/Update Settings

```bash
nano authorities/settings.py
```

Verify paths are correct:
```python
ES_HOST = "http://localhost:9200"
BATCH_SIZE = 5000
DATA_DIR = "/ix1/whcdh/data/"
```

---

## Running Ingestion Scripts

### Working Directory

```bash
cd /ix1/whcdh/elastic
```

All Python commands should be run from this directory so imports work correctly.

---

### Step 1: Create Indices (< 1 minute)

```bash
python -m processing/create_indices.py
```

**Expected output:**
```
Index 'places' already exists. Deleting...
Creating index 'places'...
Index 'places' created successfully.
Index 'toponyms' already exists. Deleting...
Creating index 'toponyms'...
Index 'toponyms' created successfully.

All indices created successfully!

Next steps:
1. Run: python -m authorities/geonames-places.py
2. Run: python -m authorities/geonames-toponyms.py
```

**Verify:**
```bash
curl http://localhost:9200/_cat/indices?v
# Should show 'places' and 'toponyms' indices
```

---

### Step 2: Index Geonames Places (2-3 hours)

```bash
python -m authorities/geonames-places.py
```

**Input file:** `/ix1/whcdh/data/geonames/allCountries/allCountries.txt`

**Expected output:**
```
Starting to index Geonames places from /ix1/whcdh/data/geonames/allCountries/allCountries.txt
Target index: places
Indexed 5000 places so far...
Indexed 10000 places so far...
...
Indexing complete. Total places indexed: 12345678
```

**Monitor progress:**
```bash
# In another tmux pane
watch -n 30 'curl -s http://localhost:9200/places/_count | jq .'
```

**If interrupted:** Just restart - Elasticsearch handles duplicate IDs gracefully (upserts)

---

### Step 3: Index Geonames Toponyms (4-6 hours)

```bash
python -m authorities/geonames-toponyms.py
```

**Input file:** `/ix1/whcdh/data/geonames/alternateNamesV2/alternateNamesV2.txt`

**Expected output:**
```
Starting to index Geonames alternate names from /ix1/whcdh/data/geonames/alternateNamesV2/alternateNamesV2.txt
Target indices: toponyms, places
Toponyms: 5000, relations: 1200, skipped: 3800
Toponyms: 10000, relations: 2400, skipped: 7600
...
Indexing complete. Toponyms: 14000000, relations: 350000, skipped: 2800000
Updating place labels with preferred names...
Updated 5000 place labels...
...
Place label update complete. Total updated: 12000000
```

**Two-phase operation:**
1. Indexes toponyms + streams wikidata/link relations to places
2. Updates place labels with preferred names

**Monitor:**
```bash
curl -s http://localhost:9200/toponyms/_count | jq .
curl -s http://localhost:9200/places/_count | jq .
```

---

### Step 4: Index Wikidata Places (8-12 hours)

```bash
python -m authorities/wikidata-places.py
```

**Input file:** `/ix1/whcdh/data/wikidata/latest-all/latest-all.json.gz`

**Expected output:**
```
Starting to index Wikidata from /ix1/whcdh/data/wikidata/latest-all/latest-all.json.gz
Target indices: places, toponyms

Note: This will take several hours for the full Wikidata dump (~110M entities)
Only geographic entities will be indexed.

Processed 100,000 entities... (places: 8234, toponyms: 42156, skipped: 49610)
Processed 200,000 entities... (places: 16789, toponyms: 86234, skipped: 96977)
...
Processed 110,000,000 entities... (places: 10456234, toponyms: 68234567, skipped: 31309199)

Indexing complete!
Total entities processed: 110,000,000
Places indexed: 10,456,234
Toponyms indexed: 68,234,567
Skipped (non-geographic): 31,309,199
```

**Progress updates every 100k entities**

**Monitor:**
```bash
# Count Wikidata places
curl -X POST http://localhost:9200/places/_count?pretty -H 'Content-Type: application/json' -d '
{
  "query": {"prefix": {"place_id": "wd:"}}
}'

# Sample Wikidata place
curl -X POST http://localhost:9200/places/_search?pretty -H 'Content-Type: application/json' -d '
{
  "query": {"prefix": {"place_id": "wd:"}},
  "size": 1
}'
```

**Long-running considerations:**
- This script takes 8-12 hours
- Keep tmux session alive or run in background
- Elasticsearch must stay running throughout
- If interrupted, restart - it will continue/update existing docs

---

### Step 5: Fetch Wikidata Geoshapes (Optional, 4-8 hours)

This step is optional but recommended for places with complex geometries (country borders, rivers, etc.)

```bash
python -m authorities/wikidata-geoshapes.py
```

**Confirmation prompt:**
```
This script fetches complex geometries (polygons/lines) for Wikidata places.
It makes API calls to Wikidata SPARQL and Wikimedia Commons.
Expected runtime: several hours with rate limiting.

Continue? (y/n):
```

Type `y` and press Enter.

**Expected output:**
```
Starting geoshape fetching and updating...
This will take several hours due to API rate limiting.

Processed: 100, Updated: 45, Skipped: 55
Processed: 200, Updated: 92, Skipped: 108
...
Processed: 10245382, Updated: 127543, Skipped: 10117839

Geoshape processing complete!
Total processed: 10,245,382
Updated with geoshapes: 127,543
Skipped: 10,117,839
```

**What it does:**
1. Scrolls through all Wikidata places
2. Queries SPARQL for P3896 (geoshape) property (~130k places)
3. Fetches GeoJSON from Wikimedia Commons
4. Computes geodetically-correct representative points
5. Updates place documents

**Rate limiting:** 0.1 second delay between requests (nice to Wikimedia)

**Monitor complex geometries:**
```bash
curl -X POST http://localhost:9200/places/_search?pretty -H 'Content-Type: application/json' -d '
{
  "query": {
    "bool": {
      "must": {"prefix": {"place_id": "wd:"}},
      "filter": {
        "nested": {
          "path": "locations",
          "query": {
            "script": {
              "script": "doc[\"locations.geometry.type\"].value != \"Point\""
            }
          }
        }
      }
    }
  },
  "size": 1
}'
```

---

## Monitoring and Troubleshooting

### Check Elasticsearch Health

```bash
# Cluster health
curl http://localhost:9200/_cluster/health?pretty

# Indices
curl http://localhost:9200/_cat/indices?v

# Document counts
curl http://localhost:9200/places/_count?pretty
curl http://localhost:9200/toponyms/_count?pretty
```

### Monitor Disk Space

```bash
df -h /ix1/whcdh/data/es-test/data/
```

Expected final size: ~150-230 GB

### Check Logs

```bash
# Elasticsearch logs
tail -f /ix1/whcdh/data/es-test/logs/elasticsearch.log

# Python script output (if redirected)
tail -f ingestion.log
```

### Script Crashes

**All scripts use streaming and can be restarted safely:**
- They process line-by-line from source files
- Elasticsearch handles duplicate IDs (updates existing)
- No need to delete indices and start over
- Just re-run the script

**Exception:** If you need to completely start over:
```bash
curl -X DELETE http://localhost:9200/places
curl -X DELETE http://localhost:9200/toponyms
python authorities/create_indices.py
# Start from Step 2
```

### Out of Memory

**Symptoms:**
- Elasticsearch crashes
- Python scripts killed
- "Out of memory" errors

**Solutions:**

1. **Request more memory:**
```bash
# Exit current session (Ctrl+d)
crc-interactive -s -c 4 -b 32 -t 48:00:00 -a whcdh  # 32GB
```

2. **Adjust ES heap size:**
```bash
# In ES startup command, change:
ES_JAVA_OPTS='-Xms8g -Xmx8g'  # For 16GB+ RAM systems
```

3. **Reduce batch size:**
```python
# Edit authorities/settings.py
BATCH_SIZE = 2500  # Reduce from 5000
```

### Elasticsearch Won't Start

**Check logs:**
```bash
cat /ix1/whcdh/data/es-test/logs/elasticsearch.log
```

**Common issues:**
- Port 9200 already in use (another ES instance running)
- Insufficient disk space
- Config file issues

**Solution:**
```bash
# Kill existing ES processes
pkill -f elasticsearch

# Check disk space
df -h /ix1/whcdh/data/

# Verify config
ls -la /ix1/whcdh/data/es-test/config/
```

### Network Timeouts

Scripts have retry logic. Transient network errors are handled automatically.

For persistent issues:
- Check CRC network status
- Verify external URLs are accessible (Wikidata, Commons)

### Python Import Errors

```bash
# Verify you're in the correct directory
pwd  # Should show /ix1/whcdh/whg-v4 or similar

# Verify Python environment
conda activate whg
python -c "from authorities.settings import ES_HOST; print(ES_HOST)"
```

### tmux Session Lost

```bash
# List sessions
tmux ls

# Reattach
tmux attach -t whg-ingest

# If session is gone, check if processes still running
ps aux | grep elasticsearch
ps aux | grep python
```

---

## Post-Ingestion Optimization

### Force Merge Indices (Recommended)

After all ingestion is complete:

```bash
# This improves query performance by consolidating segments
curl -X POST "http://localhost:9200/places/_forcemerge?max_num_segments=1"
curl -X POST "http://localhost:9200/toponyms/_forcemerge?max_num_segments=1"
```

Takes 30-60 minutes. Monitor:
```bash
curl http://localhost:9200/_cat/recovery?v
```

### Create Snapshot (Backup)

```bash
# Register repository (one-time)
curl -X PUT "http://localhost:9200/_snapshot/whg_backup" -H 'Content-Type: application/json' -d'
{
  "type": "fs",
  "settings": {
    "location": "/ix1/whcdh/data/es-test/repo"
  }
}'

# Create snapshot
curl -X PUT "http://localhost:9200/_snapshot/whg_backup/snapshot_$(date +%Y%m%d)" -H 'Content-Type: application/json' -d'
{
  "indices": "places,toponyms",
  "ignore_unavailable": true,
  "include_global_state": false
}'

# Check snapshot status
curl http://localhost:9200/_snapshot/whg_backup/_all?pretty
```

### Re-enable Replicas (If Disabled)

```bash
curl -X PUT "http://localhost:9200/places/_settings" -H 'Content-Type: application/json' -d'
{
  "index": {
    "number_of_replicas": 1
  }
}'

curl -X PUT "http://localhost:9200/toponyms/_settings" -H 'Content-Type: application/json' -d'
{
  "index": {
    "number_of_replicas": 1
  }
}'
```

---

## Expected Timeline

| Step | Runtime | Cumulative |
|------|---------|------------|
| 1. Create indices | < 1 min | 0:01 |
| 2. Geonames places | 2-3 hrs | 2:01-3:01 |
| 3. Geonames toponyms | 4-6 hrs | 6:01-9:01 |
| 4. Wikidata places | 8-12 hrs | 14:01-21:01 |
| 5. Wikidata geoshapes (optional) | 4-8 hrs | 18:01-29:01 |
| **Total (core)** | **~15-22 hrs** | |
| **Total (with geoshapes)** | **~19-30 hrs** | |

**Recommendation:** Request 48-hour session to be safe.

---

## Expected Final Results

### Document Counts

```bash
# Geonames places
curl -X POST http://localhost:9200/places/_count -H 'Content-Type: application/json' -d'
{"query": {"prefix": {"place_id": "gn:"}}}'
# Expected: ~12 million

# Wikidata places
curl -X POST http://localhost:9200/places/_count -H 'Content-Type: application/json' -d'
{"query": {"prefix": {"place_id": "wd:"}}}'
# Expected: ~10-15 million

# Total places
curl http://localhost:9200/places/_count?pretty
# Expected: ~22-27 million

# Toponyms
curl http://localhost:9200/toponyms/_count?pretty
# Expected: ~65-95 million
```

### Storage

```bash
curl http://localhost:9200/_cat/indices?v&h=index,store.size
```

Expected:
- **places**: 50-80 GB
- **toponyms**: 100-150 GB
- **Total**: 150-230 GB

---

## Quick Reference Commands

```bash
# Elasticsearch health
curl http://localhost:9200/_cluster/health?pretty

# Count documents
curl http://localhost:9200/places/_count?pretty
curl http://localhost:9200/toponyms/_count?pretty

# Sample documents
curl http://localhost:9200/places/_search?size=1&pretty
curl http://localhost:9200/toponyms/_search?size=1&pretty

# Force merge
curl -X POST "http://localhost:9200/places/_forcemerge?max_num_segments=1"

# Create snapshot
curl -X PUT "http://localhost:9200/_snapshot/whg_backup/snapshot_$(date +%Y%m%d)"

# List snapshots
curl http://localhost:9200/_snapshot/whg_backup/_all?pretty
```

---

## File Locations Reference

```
/ix1/whcdh/
├── data/
│   ├── elasticsearch-8.11.1.sif          # Singularity image
│   ├── es/
│   │   ├── data/                          # ES indices (150-230 GB)
│   │   ├── logs/                          # ES logs
│   │   ├── config/                        # ES config files
│   │   └── repo/                          # Snapshots
│   ├── geonames/
│   │   ├── allCountries/allCountries.txt # Places (~12M)
│   │   └── alternateNamesV2/alternateNamesV2.txt # Names (~17M)
│   └── wikidata/
│       └── latest-all/latest-all.json.gz  # Full dump (~110M entities)
└── whg-v4/                                # Code repository
    ├── authorities/
    │   ├── create_indices.py
    │   ├── geonames-places.py
    │   ├── geonames-toponyms.py
    │   ├── wikidata-places.py
    │   ├── wikidata-geoshapes.py
    │   ├── helpers.py
    │   └── settings.py
    └── schemas/
        ├── places.json
        └── toponyms.json
```

---

## Support and Documentation

- **CRC Documentation:** https://crc.pitt.edu/
- **Elasticsearch Docs:** https://www.elastic.co/guide/en/elasticsearch/reference/8.11/
