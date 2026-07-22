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
#         install -m 755 /vast/ishi/elastic/scripts/gaz_relay.sh ~/gaz_relay.sh
#     and point cron at ~/gaz_relay.sh (see below).
#   * It only ever runs `es.sh <fixed-arg>` for allowlisted tokens — never a string
#     taken from the request. The request token is used ONLY as a lookup key.
#   * Effective trust boundary is group `ishi` (anyone in `ishi` can drop a request).
#     That is the same group that can already edit the group-writable es.sh, so the
#     relay does not widen the trust boundary beyond what already exists.
#
# INSTALL (as gazetteer):
#   install -m 755 /vast/ishi/elastic/scripts/gaz_relay.sh ~/gaz_relay.sh
#   mkdir -p /vast/ishi/elastic/.gaz_relay && chmod 2775 /vast/ishi/elastic/.gaz_relay
#   ( crontab -l 2>/dev/null | grep -v gaz_relay; \
#     echo '* * * * * /home/gazetteer/gaz_relay.sh >> /vast/ishi/elastic/logs/gaz_relay.log 2>&1' ) | crontab -
#   # NB: after pulling an updated repo, RE-RUN the `install …` line to refresh the
#   #     running copy — cron uses ~/gaz_relay.sh, not the repo copy.
#
# A requester submits via scripts/gaz_request.sh (see that file).

set -uo pipefail

# Runtime relocated to /vast (ES + gateway off /ix1, 2026-07-15).
REPO="/vast/ishi/elastic"
RELAY="$REPO/.gaz_relay"
ES="$REPO/scripts/es.sh"
GW="$REPO/scripts/gateway_ctl.sh"

# Max wall-time for ONE service op. Without this, a hung restart holds the flock
# and wedges the relay forever (every later tick fails `flock -n` and exits) — the
# #129 failure mode. ES cold-start can take ~60s, so keep comfortably above that.
OP_TIMEOUT="${GAZ_RELAY_OP_TIMEOUT:-240}"
# Emit an "alive" heartbeat to the log at most this often (seconds) when idle, so
# the log confirms the cron is firing without a line every single minute.
HEARTBEAT_EVERY="${GAZ_RELAY_HEARTBEAT_EVERY:-900}"

# token -> FULL command (fixed HERE — the request token is only a lookup key, it
# is NEVER interpolated into the command). Gateway ops go through gateway_ctl.sh
# (the /vast-aware `gw` script), ES/Kibana ops through es.sh. This is the allowlist.
declare -A ALLOW=(
  [health]="$ES -health"
  [es-restart]="$ES es-restart"
  [es-start]="$ES es-start"
  [es-stop]="$ES es-stop"
  [kibana-restart]="$ES kibana-restart"
  [gateway-restart]="$GW restart"
  [gateway-start]="$GW start"
  [gateway-stop]="$GW stop"
  [restart-all]="$ES -restart"
)

log() { echo "$(date '+%F %T') gaz_relay: $*"; }

mkdir -p "$RELAY" 2>/dev/null
chmod 2775 "$RELAY" 2>/dev/null || true   # setgid: req/resp inherit group ishi

# single-instance lock — a restart can exceed the 1-min cron interval. flock is
# tied to the open fd, so it auto-releases if the holder dies (a stale .lock FILE
# is harmless); only a genuinely-hung op holds it — which OP_TIMEOUT now bounds.
exec 9>"$RELAY/.lock" 2>/dev/null || { log "cannot open lock file — skipping tick"; exit 0; }
if ! flock -n 9; then
  # Another run is still active (e.g. a slow restart). Log it so lag is visible.
  log "busy: previous relay run still holds the lock; skipping this tick"
  exit 0
fi

# tidy stale artefacts (>60 min) from timed-out / abandoned requests.
find "$RELAY" -maxdepth 1 -type f \( -name 'resp-*' -o -name 'req-*' -o -name '.processing-*' \) -mmin +60 -delete 2>/dev/null || true

shopt -s nullglob
pending=()
for r in "$RELAY"/req-*; do
  case "$r" in *.tmp) continue;; esac
  pending+=("$r")
done

if ((${#pending[@]} == 0)); then
  # Rate-limited heartbeat: confirms the cron is alive without spamming the log.
  hb="$RELAY/.heartbeat"
  now=$(date +%s)
  last=$(stat -c %Y "$hb" 2>/dev/null || echo 0)
  if (( now - last >= HEARTBEAT_EVERY )); then
    log "alive (no pending requests)"
    : > "$hb" 2>/dev/null || true
  fi
  exit 0
fi

for req in "${pending[@]}"; do
  id="${req##*/req-}"
  resp="$RELAY/resp-$id"
  proc="$RELAY/.processing-$id"
  requester="$(stat -c %U "$req" 2>/dev/null)"
  token="$(head -n1 "$req" 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
  mv -f "$req" "$proc" 2>/dev/null || continue   # claim it
  log "processing id=$id requester=$requester token=[$token]"
  rc=0
  {
    echo "# gaz_relay $(date '+%F %T') requester=$requester token=[$token]"
    if [[ -n "${ALLOW[$token]:-}" ]]; then
      cmd="${ALLOW[$token]}"
      echo "# running as $(whoami) (timeout ${OP_TIMEOUT}s): $cmd"
      echo "----------------------------------------"
      timeout "$OP_TIMEOUT" bash -c "$cmd"
      rc=$?
      echo "----------------------------------------"
      [[ $rc -eq 124 ]] && echo "TIMED OUT after ${OP_TIMEOUT}s (op still running or killed)"
      echo "EXIT: $rc"
    else
      rc=126
      echo "REJECTED: token not in allowlist. Allowed: ${!ALLOW[*]}"
      echo "EXIT: 126"
    fi
  } > "$resp.tmp" 2>&1
  mv -f "$resp.tmp" "$resp"
  chmod 664 "$resp" 2>/dev/null || true
  rm -f "$proc"
  log "done id=$id token=[$token] exit=$rc"
done
