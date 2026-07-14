#!/bin/bash
# gaz_request.sh — client for gaz_relay.sh. Submit ONE allowlisted service-op token
# and wait for the gazetteer-side result. Run as any group-`ishi` user (e.g. stg135).
#
#   scripts/gaz_request.sh <token> [timeout_secs]
#   tokens: health | es-restart | es-start | es-stop | kibana-restart |
#           gateway-restart | restart-all
#
# Unprivileged: it only drops a request file and polls for the response; the actual
# op runs as gazetteer via the cron-driven gaz_relay.sh (allowlist enforced there).

set -uo pipefail
RELAY="/ix1/ishi/elastic/.gaz_relay"
token="${1:?usage: gaz_request.sh <token> [timeout_secs]}"
timeout="${2:-240}"

mkdir -p "$RELAY" 2>/dev/null || true
chmod 2775 "$RELAY" 2>/dev/null || true

id="$(date +%s)-$$-${RANDOM}"
tmp="$RELAY/req-$id.tmp"
printf '%s\n' "$token" > "$tmp"
chmod 664 "$tmp" 2>/dev/null || true
mv -f "$tmp" "$RELAY/req-$id"          # atomic publish
echo "submitted req-$id token=$token — waiting up to ${timeout}s (relay polls ~1/min) ..."

waited=0
while (( waited < timeout )); do
  if [[ -f "$RELAY/resp-$id" ]]; then
    echo "===== gaz_relay response (req-$id) ====="
    cat "$RELAY/resp-$id"
    rm -f "$RELAY/resp-$id" 2>/dev/null || true
    exit 0
  fi
  sleep 3
  waited=$(( waited + 3 ))
done
echo "TIMEOUT after ${timeout}s — no response. Is the gazetteer cron for gaz_relay.sh installed and running?"
exit 1
