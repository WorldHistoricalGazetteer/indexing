#!/bin/bash
# gaz_run.sh — run a gazetteer-only service op directly, as `gazetteer`.
#
# WHY: ES, Kibana and the gateway run as `gazetteer`, and only that account can
# signal them. Until 2026-09-29 other `ishi` users reached them through gaz_relay:
# a request file dropped in a queue and a gazetteer cron job that polled it once a
# minute. That relay is retired. `stg135` has a sudo rule for exactly
# `/bin/su - gazetteer` (no arguments allowed), so the op is piped into that login
# shell on stdin and runs at once, with no queue and no polling.
#
# The token names are the relay's, so the watchdogs call this unchanged apart from
# the script name. Ops are still looked up in a fixed table: the token is only a key,
# never a command.
#
# Usage: gaz_run.sh <token> [timeout_secs]
#   tokens: health es-start es-stop es-restart kibana-restart restart-all
#           gateway-start gateway-stop gateway-restart gateway-dump
set -uo pipefail

token="${1:?usage: gaz_run.sh <token> [timeout_secs]}"
timeout="${2:-240}"

REPO=/vast/ishi/elastic
ES="$REPO/scripts/es.sh"
GW="$REPO/scripts/gateway_ctl.sh"
GWDUMP="$REPO/scripts/gateway_dump.sh"

declare -A ALLOW=(
  [health]="$ES -health"
  [es-restart]="$ES es-restart"
  [es-start]="$ES es-start"
  [es-stop]="$ES es-stop"
  [kibana-restart]="$ES kibana-restart"
  [gateway-restart]="$GW restart"
  [gateway-dump]="$GWDUMP"
  [gateway-start]="$GW start"
  [gateway-stop]="$GW stop"
  [restart-all]="$ES -restart"
)

cmd="${ALLOW[$token]:-}"
if [[ -z "$cmd" ]]; then
  echo "REJECTED: unknown token [$token]. Allowed: ${!ALLOW[*]}"
  exit 126
fi

echo "# gaz_run $(date '+%F %T') by $(id -un) token=[$token] (timeout ${timeout}s): $cmd"
if [[ "$(id -un)" == gazetteer ]]; then
  umask 002
  timeout "$timeout" bash -c "$cmd"
  rc=$?
else
  # The sudo rule matches only the bare `su - gazetteer`, so the command goes in on stdin.
  printf 'umask 002\nexec timeout %q bash -c %q\n' "$timeout" "$cmd" \
    | sudo -n /bin/su - gazetteer 2>&1 | grep -v '^Last login:'
  rc=${PIPESTATUS[1]}
fi
[[ $rc -eq 124 ]] && echo "TIMED OUT after ${timeout}s (op still running or killed)"
echo "EXIT: $rc"
exit "$rc"
