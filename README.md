# WHG Elasticsearch + Kibana

This document describes how Elasticsearch and Kibana are deployed for the World Historical Gazetteer project at University of Pittsburgh.

## Architecture Overview

| Component | Location | Purpose |
|-----------|----------|---------|
| Production ES | VM (port 9200) | Live queries, persistent |
| Staging ES | Slurm compute node (port 9201) | Indexing operations, ephemeral |
| Kibana | VM (port 5601) | Dashboard and monitoring |

### Storage Tiers

| Mount | Type | Use |
|-------|------|-----|
| `/ix1/whcdh` | Standard | Code, binaries, configs, snapshots, source data |
| `/ix3/whcdh` | Flash | Production ES data (fast queries) |
| `$SLURM_SCRATCH` | NVMe | Staging ES data (ephemeral, fast indexing) |

## Repository Setup

Clone the repository (first-time setup):

```bash
mkdir -p /ix1/whcdh
cd /ix1/whcdh
git clone git@github.com:whg/elastic.git elastic
```

Set the wrapper script as permanently executable:

```bash
chmod +x /ix1/whcdh/elastic/scripts/es.sh
git add /ix1/whcdh/elastic/scripts/es.sh
git commit -m "Make wrapper script executable"
git push origin main
```

Create an alias by adding to `~/.bashrc`:

```bash
alias es='/ix1/whcdh/elastic/scripts/es.sh'
```

Update your current shell:

```bash
source ~/.bashrc
```


> Subsequently, update to latest:
>
>```bash
>cd /ix1/whcdh/elastic
>git pull origin main
>```

## Elasticsearch Installation (Bare-Metal)

```
cd /ix1/whcdh

# --- Download Elasticsearch 9.2.1 ---
curl -L -O https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.2.1-linux-x86_64.tar.gz

# --- Extract and Rename ---
tar xf elasticsearch-9.2.1-linux-x86_64.tar.gz
mv elasticsearch-9.2.1 es-bin
rm elasticsearch-9.2.1-linux-x86_64.tar.gz

# --- Create Data Directories ---
mkdir -p /ix1/whcdh/es/{data,logs,repo,config}
```

### Kibana (Bare-Metal)

```
cd /ix1/whcdh

# --- Download Kibana 9.2.1 ---
curl -L -O https://artifacts.elastic.co/downloads/kibana/kibana-9.2.1-linux-x86_64.tar.gz

# --- Extract and Rename ---
tar xf kibana-9.2.1-linux-x86_64.tar.gz
mv kibana-9.2.1 kibana-bin
rm kibana-9.2.1-linux-x86_64.tar.gz

# --- Create Data Directories ---
mkdir -p /ix1/whcdh/kibana/{data,logs}

```

---

## Configuration

All configuration is in `.env` at the repository root:

```bash
# View current configuration
cat /ix1/whcdh/elastic/.env
```

Key variables:

| Variable | Purpose |
|----------|---------|
| `IX1_BASE` | Base path for persistent storage |
| `IX3_BASE` | Base path for flash storage |
| `PROD_ES_URL` | Production ES endpoint |
| `STAGING_ES_PORT` | Staging ES port (9201) |
| `DATA_DIR` | Authority source files |
| `SNAPSHOT_DIR` | Snapshot repository location |

## Managing Services

### Production (VM)

```bash
# Start both Elasticsearch and Kibana
es -start

# Stop both
es -stop

# Restart
es -restart

# Individual services
es es-start
es es-stop
es kibana-start
es kibana-stop
```

### Staging (Slurm)

The staging instance runs on a compute node for indexing operations.

```bash
# SSH to login node first
ssh stg135@htc.crc.pitt.edu

# Start staging ES (use 'source' to export variables)
source /ix1/whcdh/elastic/scripts/es.sh -staging-start

# Check status
source /ix1/whcdh/elastic/scripts/es.sh -staging-status

# Check health
source /ix1/whcdh/elastic/scripts/es.sh -staging-health

# View logs
source /ix1/whcdh/elastic/scripts/es.sh -staging-logs

# Stop staging ES
source /ix1/whcdh/elastic/scripts/es.sh -staging-stop
```

See [ES_STAGING.md](ES_STAGING.md) for detailed staging documentation.

## Basic Status Checks

### Production
```bash
curl -s "http://localhost:9200/_cluster/health?pretty"
curl -s "http://localhost:9200/_cat/indices?v"
```

### Staging
```bash
# After sourcing staging env
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health?pretty"
```

### Kibana
```bash
curl -s "http://localhost:5601/api/status" -H "kbn-xsrf: true"
```

## Directory Structure

```
/ix1/whcdh/
├── es-bin/                      # Elasticsearch installation
├── kibana-bin/                  # Kibana installation
├── jdk-21.0.1/                  # Java installation
├── elastic/                     # Git repository
│   ├── .env                     # Environment configuration
│   ├── scripts/
│   │   └── es.sh                # Management wrapper
│   ├── processing/
│   │   ├── es_staging.sbatch    # Staging Slurm script
│   │   ├── settings.py          # Python settings
│   │   ├── deploy_to_production.py
│   │   └── ...
│   ├── authorities/             # Ingestion scripts
│   └── schemas/                 # Index mappings
├── data/
│   └── authorities/             # Source data files
│       ├── gn/                  # GeoNames
│       ├── wd/                  # Wikidata
│       ├── tgn/                 # Getty TGN
│       └── ...
├── es/
│   ├── logs/                    # Production ES logs
│   ├── es.pid                   # Production ES PID
│   ├── staging-logs/            # Staging Slurm logs
│   └── snapshots/
│       ├── staging/             # Snapshots from staging
│       └── backup/              # Production backups
├── kibana/
│   ├── data/
│   ├── logs/
│   └── kb.pid
└── esinfo/
    └── es-staging.env           # Staging instance connection info

/ix3/whcdh/
└── es/
    └── data/                    # Production ES data (flash)

$SLURM_SCRATCH/                  # Per-job ephemeral
└── es-staging/
    ├── data/                    # Staging ES data
    ├── logs/
    └── config/
```

## Log Locations

| Service | Log Location |
|---------|--------------|
| Production ES | `/ix1/whcdh/es/logs/` |
| Staging ES (Slurm) | `/ix1/whcdh/es/staging-logs/slurm-*.out` |
| Kibana | `/ix1/whcdh/kibana/logs/` |

## Access URLs

| Service | URL | Notes |
|---------|-----|-------|
| Production ES | http://localhost:9200 | VM only |
| Staging ES | http://$ES_NODE:9201 | Compute node |
| Kibana | http://localhost:5601 | VM only |

SSH tunnel for remote Kibana access:

```bash
ssh -L 5602:localhost:5601 stg135@gazetteer.crcd.pitt.edu
# Then access: http://localhost:5602
```

## References

- [ES_STAGING.md](ES_STAGING.md) — Staging instance documentation
- [PITT_ELASTIC.md](PITT_ELASTIC.md) — Full ingestion operations guide
- [INDEX_SCHEMAS.md](INDEX_SCHEMAS.md) — Index mappings, settings, and field descriptions
- [Elasticsearch Documentation](https://www.elastic.co/guide/en/elasticsearch/reference/current/index.html)
- [Pitt CRC Documentation](https://crc.pitt.edu/)