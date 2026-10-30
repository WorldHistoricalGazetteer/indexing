#!/bin/bash
# compact_overlays_cron.sh — nightly: fold contributed-dataset geometry overlays
# into the main geom store, and delete overlays folded on an earlier night once
# every live gateway worker has moved past them (processing/compact_overlays.py).
#
# WHY A CRON AND NOT THE DATASET JOB: a VM dataset job (gateway/dataset_jobs.py)
# writes the dataset's polygons to <store>/overlays/whg-<id>/ and the gateway's
# reader serves them from there at once. Folding them into the 11.5 M-row
# index.sqlite is a separate, heavier step that rewrites a 0.4 GB file; it runs
# at night, under the SAME job lock the dataset jobs use, so the two never
# overlap. If a job is running the compaction simply exits 0 ("skipped").
#
# RUNS AS gazetteer (the gateway's user; the store, the overlays and the lock
# are all its), in the whg conda env, niced and ionice-idle so it never competes
# with production Elasticsearch on the same VM. Preflight inside the Python
# refuses when MemAvailable < 6 GB or the store filesystem has < 3 GB free.
#
# INSTALL (as gazetteer, on pitt) — one line, 03:17 daily, off the hour so it
# never lines up with the watchdogs or the weekly snapshot SLM:
#   ( crontab -l 2>/dev/null | grep -v compact_overlays_cron; \
#     echo '17 3 * * * /vast/ishi/elastic/scripts/compact_overlays_cron.sh' ) | crontab -
#
# MAINTENANCE: suppress with
#       touch /vast/ishi/elastic/.compact_overlays.disabled
#   and remove that file to re-arm. A dry run from a shell:
#       /vast/ishi/elastic/scripts/compact_overlays_cron.sh --dry-run
#
set -uo pipefail

REPO=/vast/ishi/elastic
STATE=$REPO/.compact_overlays
LOGDIR=$REPO/logs
LOG=$LOGDIR/compact_overlays.log
DISABLE_FLAG=$REPO/.compact_overlays.disabled

mkdir -p "$STATE" "$LOGDIR" 2>/dev/null || true

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG" 2>/dev/null; }

# Single-instance lock for THIS script (the Python takes the shared dataset-job
# lock itself). Mirrors gateway_watchdog.sh: never let runs stack.
exec 9>"$STATE/.lock" || exit 0
flock -n 9 || { log "another compact_overlays_cron run holds the lock — exiting."; exit 0; }

[ -f "$DISABLE_FLAG" ] && exit 0

# The whg conda env, exactly as gateway_ctl.sh's _activate_conda does it: cron's
# PATH has only system python3.9, and `python -m processing.compact_overlays`
# needs 3.11 + shapely/sqlite from the env. conda's hooks reference unbound
# vars, so relax `set -u` around the source.
CONDA_SH=/home/gazetteer/miniconda/etc/profile.d/conda.sh
if [[ -f "$CONDA_SH" ]]; then
    set +u
    # shellcheck disable=SC1090
    source "$CONDA_SH" && conda activate whg
    set -u
else
    log "ERROR: $CONDA_SH missing — cannot run the compaction."
    exit 1
fi

cd "$REPO" || { log "ERROR: cannot cd to $REPO"; exit 1; }
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

log "starting: python -m processing.compact_overlays $*"
nice -n 19 ionice -c 3 python -m processing.compact_overlays "$@" >> "$LOG" 2>&1
rc=$?
case $rc in
    0) log "finished: ok (see overlays/.last_compaction.json for folded/deleted/skipped)";;
    2) log "REFUSED by preflight (memory or disk floor) — rc=2";;
    *) log "FAILED rc=$rc — live index and overlays were left untouched; needs attention";;
esac
exit $rc
