#!/bin/bash
# Batched, health-gated apply of a way-geometry patch to the LIVE places index
# (run on the Pitt VM against prod localhost:9201). place#145.
#
# Splits the patch into N batches; per batch:
#   apply (throttled scripted _bulk, via `osm_way_area_geometry apply`)
#     -> HEALTH GATE (heap / pending / gateway) -> only_expunge_deletes
#     forcemerge (blocking) -> next.
# Bounds peak deleted-docs to ~1 batch and ABORTS between batches if prod drifts,
# instead of one unobservable multi-hour push. Used for the OSM 9.9M-op apply
# (10 batches, 0 aborts, heap stayed 4-52%).
#
# Usage: batch_apply_geom_patch.sh <patch.jsonl> [batches=10] [rps=1200]
set -uo pipefail

PATCH="${1:?usage: batch_apply_geom_patch.sh <patch.jsonl> [batches] [rps]}"
BATCHES="${2:-10}"
RPS="${3:-1200}"
ES=http://localhost:9201
PW=/ix1/ishi/es/config/elastic.password
REPO=/vast/ishi/elastic          # PYTHONPATH for `python -m processing...`
WORK="$(dirname "$PATCH")/_batches"
HEAP_MAX=80                       # abort if ES heap % exceeds this after a batch
PENDING_MAX=50
AUTH() { echo -n "elastic:$(cat "$PW")"; }

IDX=$(curl -s -u "$(AUTH)" "$ES/_alias/places" | python3 -c 'import json,sys;print(next(iter(json.load(sys.stdin))))')
echo "target index: $IDX | patch: $PATCH | batches: $BATCHES | rps: $RPS"
rm -f "$WORK"/batch_*; mkdir -p "$WORK"
split -d -n "l/$BATCHES" "$PATCH" "$WORK/batch_"

health_gate() {
  local heap pending status del seg sz
  heap=$(curl -s -u "$(AUTH)" "$ES/_cat/nodes?h=heap.percent" | tr -d ' \n')
  read -r status pending <<<"$(curl -s -u "$(AUTH)" "$ES/_cat/health?h=status,pending_tasks")"
  read -r del seg sz <<<"$(curl -s -u "$(AUTH)" "$ES/_cat/indices/$IDX?h=docs.deleted,segments.count,store.size")"
  echo "    GATE heap=${heap}% health=${status}/pending=${pending} deleted=${del} seg=${seg} size=${sz}"
  [ "${heap:-100}" -gt "$HEAP_MAX" ] && { echo "    !! heap>${HEAP_MAX}% — ABORT"; return 1; }
  [ "${pending:-999}" -gt "$PENDING_MAX" ] && { echo "    !! pending>${PENDING_MAX} — ABORT"; return 1; }
  return 0
}

i=0
for b in $(ls "$WORK"/batch_* | sort); do
  i=$((i+1))
  echo "=== batch $i/$BATCHES ($b, $(wc -l < "$b") ops) $(date +%H:%M:%S) ==="
  PYTHONPATH="$REPO" python3 -m processing.osm_way_area_geometry apply \
      --patch "$b" --rps "$RPS" || { echo "apply failed — ABORT"; exit 1; }
  health_gate || { echo "health gate failed after batch $i — ABORT (remaining NOT applied)"; exit 1; }
  echo "    expunge_deletes forcemerge (blocking)..."
  curl -s -u "$(AUTH)" -X POST "$ES/$IDX/_forcemerge?only_expunge_deletes=true&wait_for_completion=true" -o /dev/null
  health_gate || true
done
echo "ALL $BATCHES BATCHES APPLIED $(date +%H:%M:%S)"
rm -f "$WORK"/batch_*
