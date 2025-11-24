# Starting a Staging Elasticsearch Instance (Slurm)

A temporary, isolated Elasticsearch instance can be launched on a compute node using the Slurm batch script:

```
/ix1/whcdh/elastic/processing/es_staging.sbatch
```

This staging ES runs entirely in user space, uses private per-job data directories, and shuts down automatically when the job ends.

## 1. Submit the job and capture the job ID and configuration environment parameters

```
JOBID=$(sbatch --parsable /ix1/whcdh/elastic/processing/es_staging.sbatch)
echo "Launched staging ES as job $JOBID"
squeue -j "$JOBID"
INFO=/ix1/whcdh/esinfo/es-$JOBID.env
echo -n "Waiting for ES info file..."
while [ ! -f "$INFO" ]; do
    sleep 2
done
echo " ready."
source "$INFO"
# Make variables available in current shell
export ES_NODE ES_PORT ES_DATA JOBID
echo "ES Node: $ES_NODE"
echo "ES Port: $ES_PORT"
echo "ES Data Dir: $ES_DATA"
echo "ES Env File: $INFO"
```

The job environment file can be sourced by other Slurm jobs or inspected manually.

## 2. Using the staging ES in pipelines

Any job that needs to index against this ES instance can either:

* read the `es-<jobid>.env` file, or
* accept the ES node and port as parameters, or
* expect `ES_PORT` and `ES_NODE` exported via `source`:

## 3. Shutdown and cleanup

The staging instance stops automatically when the Slurm job ends.
All staging directories are deleted at job termination.

To end early, use:

```
scancel "$JOBID"
```

