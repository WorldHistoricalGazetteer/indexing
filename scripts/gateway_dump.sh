#!/bin/bash
# gateway_dump.sh — capture a stack dump of the running gateway, as `gazetteer`.
#
# WHY: when the gateway wedged on 2026-08-18 it was alive, spinning on CPU, and
# serving nothing — and the access log said nothing, because uvicorn logs a request
# only once it COMPLETES. The request that hung left no trace at all, so the cause
# could not be identified and the process had to be restarted blind.
#
# A dump names the frame every thread is sitting in, which is exactly what was
# missing. The watchdog takes one BEFORE it restarts, so the evidence survives.
#
# `--nonblocking` reads the target without pausing it: the output can be slightly
# inconsistent, but a diagnostic must never itself stall a live service.
#
# Invoked as `gazetteer` through the relay allowlist (token: gateway-dump), because
# py-spy can only attach to a process owned by the same user.
set -uo pipefail

REPO=/vast/ishi/elastic
PY_SPY=/home/gazetteer/miniconda/envs/whg/bin/py-spy
OUT_DIR="$REPO/logs"
mkdir -p "$OUT_DIR" 2>/dev/null || true

# Dump the WORKERS, not just the supervisor. Two traps here, both learned the hard
# way: with workers>1 the supervisor handles no requests at all, so a dump of it
# says nothing about a wedge; and uvicorn's workers do not carry the "python -m
# gateway" cmdline at all — multiprocessing re-executes them as `spawn_main`, so
# pgrep on the command line finds only the supervisor. Walk the process tree
# instead, and dump the supervisor too in case it is the one that is stuck.
sup="$(pgrep -f 'python -m gateway' | head -1)"
if [ -n "$sup" ]; then
    # Children, minus multiprocessing's resource_tracker (a bookkeeping helper with
    # no request state, whose stack would only add noise).
    kids="$(pgrep -P "$sup" 2>/dev/null | while read -r k; do
        grep -qa resource_tracker "/proc/$k/cmdline" 2>/dev/null || echo "$k"
    done)"
    pids="$(printf '%s\n%s\n' "$sup" "$kids" | sed '/^$/d')"
else
    pids=""
fi
if [ -z "$pids" ]; then
    echo "gateway_dump: no gateway process found — nothing to dump"
    exit 0
fi
if [ ! -x "$PY_SPY" ]; then
    echo "gateway_dump: py-spy not found at $PY_SPY — skipping dump"
    exit 0
fi

out="$OUT_DIR/gateway-dump-$(date +%Y%m%d-%H%M%S).txt"
{
    echo "# gateway stack dump — $(date '+%F %T %Z')"
    # shellcheck disable=SC2086
    ps -o pid,ppid,lstart,etime,rss,stat,cmd -p $(tr '\n' ',' <<< "$pids" | sed 's/,$//') 2>/dev/null
    for pid in $pids; do
        echo
        echo "===== pid $pid ====="
        "$PY_SPY" dump --nonblocking --pid "$pid" 2>&1
    done
} > "$out"
echo "gateway_dump: wrote $out ($(wc -l < "$out") lines)"

# Keep the last 20 dumps; they are small, but this runs unattended.
ls -1t "$OUT_DIR"/gateway-dump-*.txt 2>/dev/null | tail -n +21 | xargs -r rm -f
