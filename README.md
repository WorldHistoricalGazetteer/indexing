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

## Installation

### First-time Setup

```bash
# Clone the repository (only hardcoded path needed)
mkdir -p /ix1/whcdh
cd /ix1/whcdh
git clone git@github.com:WorldHistoricalGazetteer/elastic.git elastic

# Run installation (downloads ES + Kibana, sets up alias)
./elastic/scripts/es.sh -install

# Activate the alias in your current shell
source ~/.bashrc
```

The `-install` command:
- Creates the directory structure on ix1 and ix3
- Downloads and installs Elasticsearch and Kibana
- Makes the wrapper script executable
- Adds the `es` alias to your `.bashrc`

### Updating

```bash
es -update
```

Pulls the latest code from the main branch.

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

### VM Resource Allocation

The VM has 32GB RAM. With ES as the primary service:

| Resource | Allocation | Purpose |
|----------|------------|---------|
| ES heap | 15g | JVM heap (`-Xms15g -Xmx15g`) |
| Filesystem cache | ~15g | OS uses free RAM for caching index files |
| System/services | ~2g | OS, SSH, monitoring |

The 50/50 split between heap and filesystem cache is standard ES guidance — both are important for query performance.

This is configured in `es.sh` and applied automatically when starting production ES.

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

# Health check
es -health
```

### Staging (Slurm)

The staging instance runs on a compute node for indexing operations.

```bash
# SSH to login node first
ssh stg135@htc.crc.pitt.edu

# Start staging ES (use 'source' to export variables)
source /ix1/whcdh/elastic/scripts/es.sh -staging-start

# Health check
es -staging-health

# Check status
es -staging-status

# View logs
es -staging-logs

# Stop staging ES
source /ix1/whcdh/elastic/scripts/es.sh -staging-stop
```

See [ES_STAGING.md](ES_STAGING.md) for detailed staging documentation.

## Health Checks

```bash
# Production - full health report
es -health

# Staging - full health report  
es -staging-health
```

These show cluster health, index stats, disk usage, and memory.

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