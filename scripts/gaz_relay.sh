#!/bin/bash
# gaz_relay.sh — privilege relay for gazetteer-only service ops.
#
# WHY: ES/Kibana/gateway run as `gazetteer`, but `gazetteer` cannot be SSH'd into
# (sshd AllowUsers policy — you reach it via `su`). This relay lets group-`ishi`
# users (stg135, the Claude agent) trigger a STRICT ALLOWLIST of service ops that
# must run as `gazetteer`, without the su password and without root.
#
# SECURITY MODEL (read before installing):
#   * This script is the allowlist enforcer, so it MUST be owned by `gazetteer`
#     and NOT group/world-writable — install a copy into gazetteer's home, do NOT
#     run it directly from the group-writable repo. Install:
#         install -m 755 /ix1/ishi/elastic/scripts/gaz_relay.sh ~/gaz_relay.sh
#     and point cron at ~/gaz_relay.sh (see below).
#   * It only ever runs `es.sh <fixed-arg>` for allowlisted tokens — never a string
#     taken from the request. The request token is used ONLY as a lookup key.
#   * Effective trust boundary is group `ishi` (anyone in `ishi` can drop a request).
#     That is the same group that can already edit the group-writable es.sh, so the
#     relay does not widen the trust boundary beyond what already exists.
#
# INSTALL (as gazetteer):
#   install -m 755 /ix1/ishi/elastic/scripts/gaz_relay.sh ~/gaz_relay.sh
#   mkdir -p /ix1/ishi/elastic/.gaz_relay && chmod 2775 /ix1/ishi/elastic/.gaz_relay
#   ( crontab -l 2>/dev/null | grep -v gaz_relay; \
#     echo '* * * * * /home/gazetteer/gaz_relay.sh >> /ix1/ishi/elastic/logs/gaz_relay.log 2>&1' ) | crontab -
#
# A requester submits via scripts/gaz_request.sh (see that file).

set -uo pipefail

REPO="/ix1/ishi/elastic"
RELAY="$REPO/.gaz_relay"
ES="$REPO/scripts/es.sh"

# token -> es.sh argument. Add ops here (this is the security allowlist).
declare -A ALLOW=(
  [health]="-health"
  [es-restart]="es-restart"
  [es-start]="es-start"
  [es-stop]="es-stop"
  [kibana-restart]="kibana-restart"
  [gateway-restart]="gateway-restart"
  [restart-all]="-restart"
)

mkdir -p "$RELAY" 2>/dev/null
chmod 2775 "$RELAY" 2>/dev/null || true   # setgid: req/resp inherit group ishi

# single-instance lock — a restart can exceed the 1-min cron interval.
exec 9>"$RELAY/.lock" 2>/dev/null || exit 0
flock -n 9 || exit 0

# tidy stale artefacts (>60 min) from timed-out / abandoned requests.
find "$RELAY" -maxdepth 1 -type f \( -name 'resp-*' -o -name 'req-*' -o -name '.processing-*' \) -mmin +60 -delete 2>/dev/null || true

shopt -s nullglob
for req in "$RELAY"/req-*; do
  case "$req" in *.tmp) continue;; esac
  id="${req##*/req-}"
  resp="$RELAY/resp-$id"
  proc="$RELAY/.processing-$id"
  requester="$(stat -c %U "$req" 2>/dev/null)"
  token="$(head -n1 "$req" 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
  mv -f "$req" "$proc" 2>/dev/null || continue   # claim it
  {
    echo "# gaz_relay $(date '+%F %T') requester=$requester token=[$token]"
    if [[ -n "${ALLOW[$token]:-}" ]]; then
      arg="${ALLOW[$token]}"
      echo "# running as $(whoami): es.sh $arg"
      echo "----------------------------------------"
      bash "$ES" "$arg"
      echo "----------------------------------------"
      echo "EXIT: $?"
    else
      echo "REJECTED: token not in allowlist. Allowed: ${!ALLOW[*]}"
      echo "EXIT: 126"
    fi
  } > "$resp.tmp" 2>&1
  mv -f "$resp.tmp" "$resp"
  chmod 664 "$resp" 2>/dev/null || true
  rm -f "$proc"
done
