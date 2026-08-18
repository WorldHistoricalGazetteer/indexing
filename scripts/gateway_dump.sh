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

pid="$(pgrep -f 'python -m gateway' | head -1)"
if [ -z "$pid" ]; then
    echo "gateway_dump: no gateway process found — nothing to dump"
    exit 0
fi
if [ ! -x "$PY_SPY" ]; then
    echo "gateway_dump: py-spy not found at $PY_SPY — skipping dump"
    exit 0
fi

out="$OUT_DIR/gateway-dump-$(date +%Y%m%d-%H%M%S).txt"
{
    echo "# gateway stack dump — pid $pid — $(date '+%F %T %Z')"
    ps -o pid,lstart,etime,rss,stat,cmd -p "$pid" 2>/dev/null
    echo
    "$PY_SPY" dump --nonblocking --pid "$pid" 2>&1
} > "$out"
echo "gateway_dump: wrote $out ($(wc -l < "$out") lines)"

# Keep the last 20 dumps; they are small, but this runs unattended.
ls -1t "$OUT_DIR"/gateway-dump-*.txt 2>/dev/null | tail -n +21 | xargs -r rm -f
