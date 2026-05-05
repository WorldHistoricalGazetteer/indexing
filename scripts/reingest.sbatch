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

set -eo pipefail

NAMESPACE=${1:?"usage: sbatch reingest.sbatch <namespace>"}

source /ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh
conda activate whg
cd /vast/ishi/elastic

START_TS=$(date -u +%FT%TZ)
echo "================================================================"
echo "REINGEST namespace=$NAMESPACE  start=$START_TS  job=$SLURM_JOB_ID"
echo "================================================================"

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
