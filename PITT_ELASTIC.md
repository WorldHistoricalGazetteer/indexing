# CRC Operations Guide - WHG Elasticsearch Ingestion

Complete guide for running Elasticsearch and ingesting gazetteer data on University of Pittsburgh CRC HTC cluster.

## Table of Contents

1. [Initial Login and Setup](#initial-login-and-setup)
2. [Starting Elasticsearch](#starting-elasticsearch)
3. [Updating Code from GitHub](#updating-code-from-github)
4. [Running Ingestion Scripts](#running-ingestion-scripts)
5. [Preparing for Production](#preparing-for-production)
6. [Monitoring and Troubleshooting](#monitoring-and-troubleshooting)

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
- `-t 12:00:00`: 12 hour time limit
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
# processing/
# toponyms/
# schemas/
# README.md
# etc.

ls processing/
# Should see ingestion scripts for all authorities
```

### Check/Update Settings

```bash
nano processing/settings.py
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

### Step 1: Create Indices and Pipelines (< 1 minute)

```bash
python -m processing.create_indices
```

**Expected output:**
```
ELASTICSEARCH INDEX SETUP
Creating pipeline 'extract_namespace'...
Creating pipeline 'extract_language'...
Creating index 'places'...
Creating index 'toponyms'...
```

**Verify:**
```bash
curl http://localhost:9200/_cat/indices?v
# Should show 'places' and 'toponyms' indices
```

---

### Step 2: Index Geonames Places (2-3 hours)

```bash
python -m processing.geonames-places
```

**Input file:** `/ix1/whcdh/data/geonames/allCountries/allCountries.zip`

**Monitor progress:**
```bash
# In another tmux pane
watch -n 30 'curl -s http://localhost:9200/places/_count | jq .'
```

---

### Step 3: Index Geonames Toponyms (4-6 hours)

```bash
python -m processing.geonames-toponyms
```

**Input file:** `/ix1/whcdh/data/geonames/alternateNamesV2/alternateNamesV2.zip`

**Two-phase operation:**
1. Creates toponyms and adds relations to places
2. Updates places with toponyms arrays

---

### Step 4: Index Pleiades (30-60 minutes)

```bash
python -m processing.pleiades-places
```

**Input file:** `/ix1/whcdh/data/pleiades/pleiades-places-latest/pleiades-places-latest.json.gz`

**Note:** Uses ijson for memory-efficient streaming. Install if needed:
```bash
pip install ijson --break-system-packages
```

---

### Step 5: Index TGN (2-4 hours)

```bash
python -m processing.tgn-places
```

**Input file:** `/ix1/whcdh/data/tgn/TGNOut_PlaceMap/TGNOut_PlaceMap.zip`

**Three-phase operation:**
1. Loads coordinates from TGNOut_Coordinates.nt
2. Loads term literals from TGNOut_2Terms.nt
3. Matches places to coordinates and indexes

---

### Step 6: Index GB1900 (30-60 minutes)

```bash
python -m processing.gb1900-places
```

**Input file:** `/ix1/whcdh/data/gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip`

**Note:** CSV is UTF-16 encoded with BOM

---

### Step 7: Index Wikidata Places (8-12 hours)

```bash
python -m processing.wikidata-places
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
Geoshape references saved: 130,000
Skipped (non-geographic): 31,309,199
```

**Note:** Also saves geoshape references to `/ix1/whcdh/data/wikidata/wikidata_geoshape_refs.jsonl`

---

### Step 8: Fetch Wikidata Geoshapes (Optional, 4-8 hours)

```bash
python -m processing.wikidata-geoshapes
```

This step fetches complex geometries for Wikidata places that have them (~130k places).

---

### Step 9: Index UN Countries (< 5 minutes)

```bash
python -m processing.un-countries
```

Downloads Natural Earth data and indexes UN member countries with high-quality boundary geometries.

---

## Preparing for Production

After all ingestion is complete, prepare indices for production use:

```bash
python -m processing.prepare_for_production
```

This script will:
1. Update `number_of_replicas` from 0 to 1
2. Update `refresh_interval` from -1 to 1s
3. Force merge indices to optimize segments (30-60 minutes)
4. Create a snapshot backup

**Interactive prompts:**
```
PREPARE INDICES FOR PRODUCTION
================================================================================

Checking indices...
  ✓ Index 'places' exists
  ✓ Index 'toponyms' exists

--- Current Statistics ---

places Statistics:
  Documents: 35,456,234
  Size: 85.23 GB
  Replicas: 0
  Refresh interval: -1

toponyms Statistics:
  Documents: 145,234,567
  Size: 142.45 GB
  Replicas: 0
  Refresh interval: -1

--------------------------------------------------------------------------------
This script will:
1. Update number_of_replicas from 0 to 1
2. Update refresh_interval from -1 to 1s
3. Force merge indices to optimize segments
4. Create a snapshot backup
--------------------------------------------------------------------------------

Proceed with production preparation? (y/n): y

--- Updating Settings ---
Updating places settings...
  ✓ Updated replicas and refresh interval
Updating toponyms settings...
  ✓ Updated replicas and refresh interval

--- Force Merging ---
This may take 30-60 minutes...
Force merging places...
  ✓ Force merge complete
Force merging toponyms...
  ✓ Force merge complete

--- Creating Backup ---
Creating snapshot: production_20250122_143022
  ✓ Snapshot created: production_20250122_143022

--- Final Statistics ---

places Statistics:
  Documents: 35,456,234
  Size: 85.23 GB
  Replicas: 1
  Refresh interval: 1s

toponyms Statistics:
  Documents: 145,234,567
  Size: 142.45 GB
  Replicas: 1
  Refresh interval: 1s

================================================================================
PRODUCTION PREPARATION COMPLETE
================================================================================

Your indices are now ready for production use!
```

### Manual Settings Update (Alternative)

If you prefer to update settings manually:

```bash
# Update places index
curl -X PUT "http://localhost:9200/places/_settings" -H 'Content-Type: application/json' -d'
{
  "index": {
    "number_of_replicas": 1,
    "refresh_interval": "1s"
  }
}'

# Update toponyms index
curl -X PUT "http://localhost:9200/toponyms/_settings" -H 'Content-Type: application/json' -d'
{
  "index": {
    "number_of_replicas": 1,
    "refresh_interval": "1s"
  }
}'

# Force merge (optional but recommended)
curl -X POST "http://localhost:9200/places/_forcemerge?max_num_segments=1"
curl -X POST "http://localhost:9200/toponyms/_forcemerge?max_num_segments=1"
```

**Input file:** `/ix1/whcdh/data/gb1900/GB1900_gazetteer_abridged_july_2018/GB1900_gazetteer_abridged_july_2018.zip`

**Note:** Contains ~1.5M British place names from 1888-1914

---

### Step 7: Index UN Countries (5 minutes)

```bash
python -m processing.un-countries
```

**Note:** Downloads Natural Earth data automatically if not present. Includes country boundaries and ISO codes.

---

### Step 8: Index Wikidata Places (8-12 hours)

```bash
python -m processing.wikidata-places
```

**Input file:** `/ix1/whcdh/data/wikidata/latest-all/latest-all.json.gz`

**Expected output:**
```
Starting to index Wikidata from /ix1/whcdh/data/wikidata/latest-all/latest-all.json.gz
Target indices: places, toponyms

Note: This will take several hours for the full Wikidata dump (~110M entities)
Only geographic entities will be indexed.

Processed 100,000 entities... (places: 8234, toponyms: 42156, skipped: 49610)
...
```

**Creates:** Reference file for geoshapes at `/ix1/whcdh/data/wikidata/wikidata_geoshape_refs.jsonl`

---

### Step 9: Fetch Wikidata Geoshapes (Optional, 4-8 hours)

This step fetches complex geometries (polygons/lines) for Wikidata places.

```bash
python -m processing.wikidata-geoshapes
```

**Confirmation prompt:**
```
This script fetches complex geometries (polygons/lines) for Wikidata places.
It makes API calls to Wikidata SPARQL and Wikimedia Commons.
Expected runtime: several hours with rate limiting.

Continue? (y/n):
```

Type `y` and press Enter.

**Note:** This script is resumable - it logs progress and can be restarted if interrupted.

---

### Step 10: Generate Phonetic Features (Optional, 4-6 hours)

#### PanPhon Features
```bash
# Install dependencies if needed
pip install epitran panphon --break-system-packages

# Generate IPA and PanPhon features
python -m toponyms.generate_phonetic_features
```

#### BiLSTM Embeddings (requires trained model)
```bash
python -m toponyms.generate_bilstm_embeddings --model /path/to/model.pt
```

---

## Preparing for Production

After all ingestion is complete, prepare indices for production use:

### Run Production Preparation Script

```bash
python -m processing.prepare_for_production
```

This script will:
1. Update `number_of_replicas` from 0 to 1
2. Update `refresh_interval` from -1 to 1s
3. Force merge indices to optimize segments
4. Create a snapshot backup

**Expected dialogue:**
```
PREPARE INDICES FOR PRODUCTION
================================================================================

Checking indices...
  ✓ Index 'places' exists
  ✓ Index 'toponyms' exists

--- Current Statistics ---

places Statistics:
  Documents: 22,456,234
  Size: 65.34 GB
  Replicas: 0
  Refresh interval: -1

toponyms Statistics:
  Documents: 78,234,567
  Size: 125.67 GB
  Replicas: 0
  Refresh interval: -1

--------------------------------------------------------------------------------
This script will:
1. Update number_of_replicas from 0 to 1
2. Update refresh_interval from -1 to 1s
3. Force merge indices to optimize segments
4. Create a snapshot backup
--------------------------------------------------------------------------------

Proceed with production preparation? (y/n): y

--- Updating Settings ---
Updating places settings...
  ✓ Updated replicas and refresh interval
Updating toponyms settings...
  ✓ Updated replicas and refresh interval

--- Force Merging ---
This may take 30-60 minutes...
Force merging places...
  ✓ Force merge complete
Force merging toponyms...
  ✓ Force merge complete

--- Creating Backup ---

Creating snapshot: production_20250122_143045
  ✓ Snapshot created: production_20250122_143045

--- Final Statistics ---

places Statistics:
  Documents: 22,456,234
  Size: 65.34 GB
  Replicas: 1
  Refresh interval: 1s

toponyms Statistics:
  Documents: 78,234,567
  Size: 125.67 GB
  Replicas: 1
  Refresh interval: 1s

================================================================================
PRODUCTION PREPARATION COMPLETE
================================================================================

Your indices are now ready for production use!
```

### Manual Settings Update (Alternative)

If you prefer to update settings manually:

```bash
# Update places index
curl -X PUT "http://localhost:9200/places/_settings" -H 'Content-Type: application/json' -d'
{
  "index": {
    "number_of_replicas": 1,
    "refresh_interval": "1s"
  }
}'

# Update toponyms index
curl -X PUT "http://localhost:9200/toponyms/_settings" -H 'Content-Type: application/json' -d'
{
  "index": {
    "number_of_replicas": 1,
    "refresh_interval": "1s"
  }
}'
```

### Force Merge for Performance

```bash
# This improves query performance by consolidating segments
curl -X POST "http://localhost:9200/places/_forcemerge?max_num_segments=1"
curl -X POST "http://localhost:9200/toponyms/_forcemerge?max_num_segments=1"
```

Takes 30-60 minutes. Monitor:
```bash
curl http://localhost:9200/_cat/recovery?v
```

### Create Production Snapshot

```bash
# Register repository (one-time)
curl -X PUT "http://localhost:9200/_snapshot/whg_backup" -H 'Content-Type: application/json' -d'
{
  "type": "fs",
  "settings": {
    "location": "/ix1/whcdh/es/repo"
  }
}'

# Create snapshot
curl -X PUT "http://localhost:9200/_snapshot/whg_backup/production_$(date +%Y%m%d)" -H 'Content-Type: application/json' -d'
{
  "indices": "places,toponyms",
  "ignore_unavailable": true,
  "include_global_state": false
}'

# Check snapshot status
curl http://localhost:9200/_snapshot/whg_backup/_all?pretty
```

---

## Monitoring and Troubleshooting

### Check Elasticsearch Health

```bash
# Cluster health
curl http://localhost:9200/_cluster/health?pretty

# Indices
curl http://localhost:9200/_cat/indices?v

# Document counts by source
curl -X POST http://localhost:9200/places/_count?pretty -H 'Content-Type: application/json' -d'
{"query": {"prefix": {"place_id": "gn:"}}}'
# Repeat for other prefixes: wd:, pl:, tgn:, gb1900:, un:
```

### Monitor Disk Space

```bash
df -h /ix1/whcdh/es/data/
```

Expected final size: ~200-300 GB total

### Check Logs

```bash
# Elasticsearch logs
tail -f /ix1/whcdh/es/logs/elasticsearch.log

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
python -m processing.create_indices
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
ES_JAVA_OPTS='-Xms8g -Xmx8g'  # For 32GB+ RAM systems
```

3. **Reduce batch size:**
```python
# Edit processing/settings.py
BATCH_SIZE = 2500  # Reduce from 5000
```

---

## Expected Timeline

| Step | Script | Runtime | Documents (approx) |
|------|-----------|---------|-------------------|
| 1 | create_indices | < 1 min | - |
| 2 | geonames-places | 2-3 hrs | ~12 million |
| 3 | geonames-toponyms | 4-6 hrs | ~17 million |
| 4 | pleiades-places | 30-60 min | ~37,000 |
| 5 | tgn-places | 2-4 hrs | ~2.5 million |
| 6 | gb1900-places | 30-60 min | ~1.5 million |
| 7 | un-countries | 5 min | ~200 |
| 8 | wikidata-places | 8-12 hrs | ~10-15 million |
| 9 | wikidata-geoshapes (optional) | 4-8 hrs | ~130,000 updates |
| 10 | prepare_for_production | 30-60 min | Settings update |
| **Total (core)** | | **~18-26 hrs** | |
| **Total (with optional)** | | **~22-34 hrs** | |

**Recommendation:** Request 48-hour session to be safe.

---

## Expected Final Results

### Document Counts

```bash
# Total places
curl http://localhost:9200/places/_count?pretty
# Expected: ~25-30 million

# Total toponyms  
curl http://localhost:9200/toponyms/_count?pretty
# Expected: ~80-100 million
```

### Storage

```bash
curl http://localhost:9200/_cat/indices?v&h=index,store.size
```

Expected:
- **places**: 60-90 GB
- **toponyms**: 120-180 GB
- **Total**: 180-270 GB

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

# Update settings for production
curl -X PUT "http://localhost:9200/places/_settings" -H 'Content-Type: application/json' -d'
{"index": {"number_of_replicas": 1, "refresh_interval": "1s"}}'

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
│   │   ├── data/                          # ES indices (180-270 GB)
│   │   ├── logs/                          # ES logs
│   │   ├── config/                        # ES config files
│   │   └── repo/                          # Snapshots
│   ├── geonames/
│   │   ├── allCountries/allCountries.zip  # Places (~12M)
│   │   └── alternateNamesV2/alternateNamesV2.zip # Names (~17M)
│   ├── wikidata/
│   │   ├── latest-all/latest-all.json.gz  # Full dump (~110M entities)
│   │   └── wikidata_geoshape_refs.jsonl   # Geoshape references
│   ├── pleiades/
│   │   └── pleiades-places-latest/pleiades-places-latest.json.gz
│   ├── tgn/
│   │   └── TGNOut_PlaceMap/TGNOut_PlaceMap.zip
│   ├── gb1900/
│   │   └── GB1900_gazetteer_abridged_july_2018/
│   └── natural_earth/
│       └── ne_10m_admin_0_countries.zip
└── elastic/                               # Code repository
    ├── processing/
    │   ├── create_indices.py
    │   ├── prepare_for_production.py
    │   ├── geonames-places.py
    │   ├── geonames-toponyms.py
    │   ├── pleiades-places.py
    │   ├── tgn-places.py
    │   ├── gb1900-places.py
    │   ├── wikidata-places.py
    │   ├── wikidata-geoshapes.py
    │   ├── un-countries.py
    │   ├── helpers.py
    │   ├── settings.py
    │   └── utilities.py
    ├── toponyms/
    │   ├── generate_phonetic_features.py
    │   └── generate_bilstm_embeddings.py
    └── schemas/
        ├── places.json
        ├── places_pipeline.json
        ├── toponyms.json
        └── toponyms_pipeline.json
```

---

## Support and Documentation

- **CRC Documentation:** https://crc.pitt.edu/
- **Elasticsearch Docs:** https://www.elastic.co/guide/en/elasticsearch/reference/8.11/
- **Project Repository:** https://github.com/WorldHistoricalGazetteer/whg-v4