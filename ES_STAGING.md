# Staging Elasticsearch Instance

A single staging Elasticsearch instance runs on a Slurm compute node for indexing operations. This instance is **ephemeral** — data lives on fast local NVMe storage and is destroyed when the job ends.

## Architecture

| Aspect | Detail |
|--------|--------|
| Instance count | One at a time (all jobs share it) |
| Port | 9201 (fixed) |
| Data storage | `$SLURM_SCRATCH` (ephemeral NVMe) |
| Snapshot storage | `/ix1/whcdh/es/snapshots/staging` (persistent) |
| Max runtime | 48 hours |

## Starting the Staging Instance

From a CRC login node:

```bash
ssh stg135@htc.crc.pitt.edu
source /ix1/whcdh/elastic/scripts/es.sh -staging-start
```

This will:
1. Submit a Slurm job to a compute node
2. Start Elasticsearch with data on local NVMe
3. Register the staging snapshot repository
4. Restore the latest snapshots (if any exist)
5. Configure indices for bulk indexing (`refresh_interval: -1`)
6. Export environment variables to your shell

Connection info is written to `/ix1/whcdh/esinfo/es-staging.env`.

## Using the Staging Instance

### In the current shell

After starting, environment variables are exported:

```bash
echo "ES available at: http://$ES_NODE:$ES_PORT"
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health?pretty"
```

### In other shells or scripts

Source the environment file:

```bash
source /ix1/whcdh/esinfo/es-staging.env
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health?pretty"
```

### In Slurm batch jobs

Jobs that index against staging should check that staging is running:

```bash
#!/bin/bash
#SBATCH ...

STAGING_ENV="/ix1/whcdh/esinfo/es-staging.env"

if [ ! -f "$STAGING_ENV" ]; then
    echo "ERROR: No staging ES instance running"
    echo "Start one with: source /ix1/whcdh/elastic/scripts/es.sh -staging-start"
    exit 1
fi

source "$STAGING_ENV"
echo "Using staging ES at http://$ES_NODE:$ES_PORT"

# Your indexing commands here...
```

## Creating Snapshots

Snapshots must be created **explicitly** after completing a logical unit of work. They are **not** created automatically on shutdown.

### Create a checkpoint snapshot

```bash
SNAPSHOT_NAME="checkpoint_$(date +%Y%m%d_%H%M%S)"
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/$SNAPSHOT_NAME?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
    "indices": "places,toponyms",
    "ignore_unavailable": true,
    "include_global_state": false
}'
```

### List existing snapshots

```bash
curl -s "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/_all?pretty"
```

### Delete old snapshots

```bash
curl -X DELETE "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/old_snapshot_name"
```

## Checking Status

```bash
source /ix1/whcdh/elastic/scripts/es.sh -staging-status
source /ix1/whcdh/elastic/scripts/es.sh -staging-health
source /ix1/whcdh/elastic/scripts/es.sh -staging-logs
```

## Stopping the Staging Instance

```bash
source /ix1/whcdh/elastic/scripts/es.sh -staging-stop
```

This will:
1. Prompt for confirmation (data will be lost)
2. Cancel the Slurm job
3. Clean up the ephemeral data directory
4. Remove the environment file

**Important**: Create snapshots of any work you want to keep *before* stopping.

## Handling Job Timeouts

The staging instance has a 48-hour time limit. If your work might exceed this:

1. **Break work into phases** that complete within the time limit
2. **Create explicit snapshots** after each phase completes
3. **Start a new staging instance** — snapshots restore automatically

If the job times out mid-operation:
- Uncommitted work on the local NVMe is lost
- The last explicit snapshot is preserved
- Start a new instance to continue from the last checkpoint

## Index Settings for Bulk Indexing

The staging sbatch automatically configures indices for fast bulk indexing:

```json
{
  "index": {
    "refresh_interval": "-1",
    "number_of_replicas": 0
  }
}
```

Before transferring to production, `deploy_to_production.py` resets these to query-optimized values.

## Workflow Example

```bash
# Day 1: Start staging, ingest GeoNames
source /ix1/whcdh/elastic/scripts/es.sh -staging-start

cd /ix1/whcdh/elastic
python -m authorities.geonames_places      # Includes checkpoint snapshot
python -m authorities.geonames_toponyms    # Includes checkpoint snapshot

source /ix1/whcdh/elastic/scripts/es.sh -staging-stop

# Day 2: Continue with Wikidata
source /ix1/whcdh/elastic/scripts/es.sh -staging-start
# Latest snapshots restored automatically

python -m authorities.wikidata_places      # Includes checkpoint snapshot

source /ix1/whcdh/elastic/scripts/es.sh -staging-stop

# Day 3: Generate embeddings, deploy to production
source /ix1/whcdh/elastic/scripts/es.sh -staging-start

python -m toponyms.generate_bilstm_embeddings

# Create final snapshot
curl -X PUT "http://$ES_NODE:$ES_PORT/_snapshot/staging_repo/complete_$(date +%Y%m%d)?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{"indices": "places,toponyms"}'

source /ix1/whcdh/elastic/scripts/es.sh -staging-stop

# Deploy to production (run on VM)
cd /ix1/whcdh/elastic
python -m processing.deploy_to_production
```

## Storage Locations

| Path | Purpose | Persistence |
|------|---------|-------------|
| `$SLURM_SCRATCH/es-staging/data` | ES index data | Ephemeral (job lifetime) |
| `$SLURM_SCRATCH/es-staging/logs` | ES logs | Ephemeral |
| `/ix1/whcdh/es/snapshots/staging` | Snapshots | Persistent |
| `/ix1/whcdh/es/staging-logs` | Slurm job logs | Persistent |
| `/ix1/whcdh/esinfo/es-staging.env` | Connection info | While job running |

## Troubleshooting

### Staging won't start

Check recent Slurm logs:

```bash
ls -lt /ix1/whcdh/es/staging-logs/*.out | head -5
tail -100 /ix1/whcdh/es/staging-logs/slurm-JOBID.out
```

### Stale environment file

If the staging env file exists but the job isn't running:

```bash
rm /ix1/whcdh/esinfo/es-staging.env
source /ix1/whcdh/elastic/scripts/es.sh -staging-start
```

### Out of memory

Edit `es_staging.sbatch` to request more memory:

```bash
#SBATCH --mem=32G
```

And increase JVM heap:

```bash
export ES_JAVA_OPTS="-Xms12g -Xmx12g"
```

### Connection refused

Verify the job is still running:

```bash
source /ix1/whcdh/esinfo/es-staging.env
squeue -j $SLURM_JOB_ID
```

Check if ES is listening:

```bash
curl -s "http://$ES_NODE:$ES_PORT/_cluster/health"
```