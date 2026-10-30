#!/bin/bash
#SBATCH --partition=htc
#SBATCH --qos=htc-htc-s
#SBATCH --time=24:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output=/vast/ishi/elastic/logs/reingest-%x-%j.out
#SBATCH --error=/vast/ishi/elastic/logs/reingest-%x-%j.err

# scripts/reingest.sbatch — admin-triggered authority refresh.
#
# Submitted by the gateway endpoint POST /api/registry/reingest. The
# gateway sets --job-name=reingest-<namespace> (used by squeue for the
# concurrency guard) and passes the namespace as $1.
#
# Stages run inside this single job (so the gateway has one job_id to
# track end-to-end, satisfying the ``GET /api/registry/reingest/<id>``
# contract):
#
#   1. fetch_authorities --age 0 --namespace <ns>      (pull latest source)
#   2. WHG_STAGING_MODE=1 python authorities/<ns>-places.py
#                                                       (re-extract to staged)
#
# Downstream pipeline stages (boundary, h3, ccode, aat_enrich,
# temporal_extent, toponyms-rebuild, ES re-index, tile rebuild) are NOT
# automated here. They are triggered by the operator using the existing
# submit_*_slurm scripts once the staging files have landed. Encoding the
# full chain inside this single job would force 24h walls for the slow
# namespaces and nest dependency logic that's already first-class in
# the per-stage submitters; the per-namespace re-ingest contract only
# guarantees that fresh source data is restaged.
#
# ⚠ DEPRECATED (30 Oct 2026): the ``whg:<id>`` branch below is NO LONGER what
# the gateway runs. ``POST /api/registry/reingest`` with ``whg:<id>`` now
# launches ``python -m processing.local_dataset_job`` ON THE GATEWAY VM
# (``gateway/dataset_jobs.py``), because the VM has no Slurm access and the
# htc compute nodes cannot reach prod ES — this branch could only ever stage
# and then print the index command for an operator to run from pitt. It is
# kept, unchanged, for a manual ``sbatch`` by an operator; nothing submits it
# with a ``whg:<id>`` argument any more. The plain-namespace path below it is
# also no longer reachable from the gateway (501) unless
# ``REINGEST_PLAIN_VIA_SLURM=1`` is set in the gateway's environment.
#
# ONE CONTRIBUTED DATASET (place#341): $1 may be ``whg:<id>`` (the gateway
# validates ^whg:[0-9]+$ and names the job reingest-whg-<id>). That branch
#
#   1. stages that dataset alone:   authorities/whg-places.py --dataset <id>
#      (no fetch_authorities — whg is pulled from the Django API, not a file)
#   2. runs the per-dataset index step:
#        python -m processing.index_namespace --namespace whg --dataset <id> --replace
#      which is a DRY-RUN unless REINGEST_EXECUTE=1 is set in the job's
#      environment (sbatch --export=ALL,REINGEST_EXECUTE=1, or the gateway's
#      environment). The index step talks to Elasticsearch at
#      ${REINGEST_ES_HOST:-http://localhost:9201}; htc compute nodes CANNOT
#      reach prod ES (measured 21 Sep 2026), so when that host does not answer
#      the step is skipped with the exact command to run from pitt printed
#      instead, and the job still exits 0 because the staging half — the
#      contract this job guarantees — has landed. The index step itself
#      refuses a dataset whose staged sidecar is not dataset_status=published.
#   3. the index step is passed --job-id "$SLURM_JOB_ID" so that, after a
#      ZERO-ERROR EXECUTE run, it writes staged/whg/datasets/<id>/indexed.json
#      stamped with this job. GET /api/registry/reingest/<job_id> reports
#      indexed=true only when that marker names the polled job; a dry run, a
#      skipped index step, or a bulk with errors leaves no marker, and the
#      gateway keeps reporting indexed=false — "job exited 0" and "dataset
#      is indexed" are deliberately two different signals.
#
# Other namespaces' behaviour is unchanged.

set -eo pipefail

NAMESPACE=${1:?"usage: sbatch reingest.sbatch <namespace|whg:<id>>"}

source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
cd /vast/ishi/elastic

START_TS=$(date -u +%FT%TZ)
echo "================================================================"
echo "REINGEST namespace=$NAMESPACE  start=$START_TS  job=$SLURM_JOB_ID"
echo "================================================================"

# ---------------------------------------------------------------------------
# Contributed-dataset branch: whg:<id>
# ---------------------------------------------------------------------------
case "$NAMESPACE" in
    whg:*)
        DATASET_ID=${NAMESPACE#whg:}
        # Re-validate on this side too: the gateway is the only caller today,
        # but this script is the thing that actually runs with the value.
        if ! [[ "$DATASET_ID" =~ ^[0-9]+$ ]]; then
            echo "ERROR: malformed dataset id in '$NAMESPACE' (want whg:<digits>)" >&2
            exit 2
        fi
        ES_HOST_FOR_INDEX=${REINGEST_ES_HOST:-http://localhost:9201}
        # --job-id travels with the command in BOTH paths (run here, or handed
        # off to pitt) so the marker, whoever writes it, names THIS job — the
        # id the gateway will be polled with.
        INDEX_CMD=(python -m processing.index_namespace --namespace whg
                   --dataset "$DATASET_ID" --replace --es-host "$ES_HOST_FOR_INDEX"
                   --job-id "$SLURM_JOB_ID")
        if [ "${REINGEST_EXECUTE:-0}" = "1" ]; then
            INDEX_CMD+=(--execute)
            INDEX_MODE="EXECUTE"
        else
            INDEX_MODE="DRY-RUN (set REINGEST_EXECUTE=1 to apply)"
        fi

        echo
        echo "--- Step 1/2: stage dataset whg:$DATASET_ID ---"
        PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}" \
            WHG_STAGING_MODE=1 python authorities/whg-places.py --dataset "$DATASET_ID"

        echo
        echo "--- Step 2/2: index dataset whg:$DATASET_ID  [$INDEX_MODE] ---"
        echo "indexed.json is written only after a zero-error execute run; until then"
        echo "the gateway reports indexed=false  (job_id=$SLURM_JOB_ID)"
        if curl -s -o /dev/null --max-time 10 "$ES_HOST_FOR_INDEX"; then
            "${INDEX_CMD[@]}"
        else
            echo "NOTE: Elasticsearch at $ES_HOST_FOR_INDEX is not reachable from"
            echo "      $(hostname) (htc compute nodes cannot reach prod ES)."
            echo "      Staging is complete; run the index step from pitt:"
            echo "        cd /vast/ishi/elastic && ${INDEX_CMD[*]}"
            echo "      indexed.json is written only after a zero-error execute run; until then"
            echo "      the gateway reports indexed=false."
        fi

        END_TS=$(date -u +%FT%TZ)
        echo
        echo "================================================================"
        echo "REINGEST namespace=$NAMESPACE  end=$END_TS  job=$SLURM_JOB_ID  OK"
        echo "================================================================"
        exit 0
        ;;
esac

# Step 1 — refresh source. fetch_authorities is per-namespace and
# returns non-zero only on hard failures; treat soft "nothing to fetch"
# as informational so the script doesn't abort on a no-op refresh.
echo
echo "--- Step 1/2: refresh authority source data ---"
if ! python -m processing.fetch_authorities --namespace "$NAMESPACE" --age 0; then
    echo "WARNING: fetch_authorities returned non-zero — proceeding with"
    echo "the existing on-disk source. This is the right call when the"
    echo "remote is temporarily unreachable but a recent download is"
    echo "still on disk; the extract step will surface a real failure"
    echo "if the data is genuinely missing."
fi

# Step 2 — re-run the staged extract for this namespace.
#
# The authority namespace (used as the place_id prefix) usually differs
# from the source-name in the script filename (e.g. ``clio`` extracts
# from ``cliopatria-places.py``). The default fallback handles the
# happy case where they match (``osm`` → ``osm-places.py``).
echo
echo "--- Step 2/2: staged extract ---"
case "$NAMESPACE" in
    clio) SCRIPT="authorities/cliopatria-places.py" ;;
    po)   SCRIPT="authorities/periodo-places.py" ;;
    nl)   SCRIPT="authorities/nativeland-places.py" ;;
    pl)   SCRIPT="authorities/pleiades-places.py" ;;
    gn)   SCRIPT="authorities/geonames-places.py" ;;
    gb)   SCRIPT="authorities/gb1900-places.py" ;;
    iv)   SCRIPT="authorities/indexvillaris-places.py" ;;
    dp)   SCRIPT="authorities/dplace-places.py" ;;
    wd)   SCRIPT="authorities/wikidata-places.py" ;;
    un)   SCRIPT="authorities/un-countries.py" ;;
    *)    SCRIPT="authorities/${NAMESPACE}-places.py" ;;
esac
if [ ! -f "$SCRIPT" ]; then
    echo "ERROR: extract script not found: $SCRIPT" >&2
    exit 2
fi
# Authority scripts live under ``authorities/`` and ``import processing.*``.
# Running them as plain scripts puts only ``authorities/`` on sys.path, so
# pin PYTHONPATH to the repo root.
PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}" \
    WHG_STAGING_MODE=1 python "$SCRIPT"

END_TS=$(date -u +%FT%TZ)
echo
echo "================================================================"
echo "REINGEST namespace=$NAMESPACE  end=$END_TS  job=$SLURM_JOB_ID  OK"
echo "================================================================"
