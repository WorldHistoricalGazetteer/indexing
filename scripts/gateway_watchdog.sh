#!/bin/bash
# gateway_watchdog.sh — restart the API gateway when it stops serving.
#
# WHY: on 2026-08-18 the gateway wedged — process alive, spinning on CPU, RSS ~2GB,
# answering nothing on 9200 — and stayed that way until a human noticed. Because
# Django reaches the legacy `whg,pub,wdgn` indexes THROUGH the gateway (port 9200,
# not ES's 9201 directly), that outage took reconciliation, search and the Atlas
# gazetteer down on production as well as dev. The existing es_watchdog.sh did not
# help: Elasticsearch itself was healthy the whole time.
#
# LIVENESS, NOT PROCESS-LIVENESS: `pgrep` would have found the wedged gateway and
# concluded all was well. The only meaningful test is whether it answers HTTP, so
# this probes /openapi.json (unauthenticated, cheap, 200 when serving).
#
# EVIDENCE FIRST: a wedge leaves no trace — uvicorn's access log records a request
# only when it completes, so the request that hung is invisible. The watchdog
# therefore submits `gateway-dump` (py-spy) BEFORE the restart, so the next
# occurrence can be diagnosed instead of merely survived.
#
# ANTI-THRASH: FAIL_THRESHOLD consecutive failures before acting, a COOLDOWN
# between restarts, a single-instance lock, and it stands down if a relay request
# is already queued. Mirrors es_watchdog.sh deliberately — one pattern to learn.
#
# MAINTENANCE: suppress with
#       touch /vast/ishi/elastic/.gateway_watchdog.disabled
#   and remove that file to re-arm.
#
# INSTALL (as stg135, on pitt):
#   ( crontab -l 2>/dev/null | grep -v gateway_watchdog; \
#     echo '*/2 * * * * /vast/ishi/elastic/scripts/gateway_watchdog.sh' ) | crontab -
#
set -uo pipefail

REPO=/vast/ishi/elastic
GW_URL=http://localhost:9200/openapi.json
RELAY=$REPO/.gaz_relay
STATE=$REPO/.gateway_watchdog
LOG=$REPO/logs/gateway_watchdog.log
DISABLE_FLAG=$REPO/.gateway_watchdog.disabled

FAIL_THRESHOLD=2      # consecutive failing checks (≈4 min at */2) before acting
COOLDOWN=600          # seconds after a restart before trying again
PROBE_TIMEOUT=8       # a healthy gateway answers this in milliseconds

mkdir -p "$STATE" 2>/dev/null || true
FAILCT=$STATE/failcount
LASTSTART=$STATE/laststart

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG" 2>/dev/null; }

# Single-instance lock: a gaz_request submission can block for minutes, longer than
# the cron interval — don't let runs stack.
exec 9>"$STATE/.lock" || exit 0
flock -n 9 || exit 0

[ -f "$DISABLE_FLAG" ] && exit 0

# --- liveness probe ---
# curl prints "000" on refusal/timeout and still writes it to stdout while exiting
# non-zero, so never append `|| echo 000` — that would make a down gateway read as up.
code=$(curl -s -m "$PROBE_TIMEOUT" -o /dev/null -w '%{http_code}' "$GW_URL" 2>/dev/null)
[ -z "$code" ] && code=000

# A WEDGED gateway accepts the connection and never replies, so the timeout shows as
# 000 exactly like a dead one; either way it is not serving. Any real HTTP status
# (even 401/404) means the event loop is turning, which is what we care about.
if [ "$code" != "000" ]; then
    [ -f "$FAILCT" ] && rm -f "$FAILCT" 2>/dev/null
    exit 0
fi

n=$(( $(cat "$FAILCT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAILCT"
log "gateway not serving on 9200 (curl_code=$code) — consecutive_failures=$n"

[ "$n" -lt "$FAIL_THRESHOLD" ] && exit 0

now=$(date +%s)
last=$(cat "$LASTSTART" 2>/dev/null || echo 0)
if [ $(( now - last )) -lt "$COOLDOWN" ]; then
    log "within cooldown ($(( now - last ))s < ${COOLDOWN}s since last restart) — waiting."
    exit 0
fi

if ls "$RELAY"/req-* "$RELAY"/.processing-* >/dev/null 2>&1; then
    log "a gaz_relay request is already pending/processing — skipping submission."
    exit 0
fi

echo "$now" > "$LASTSTART"

# Evidence before recovery: what was it doing? Short timeout — a dump is worth
# having, but never at the cost of delaying the restart.
log "gateway down for $n consecutive checks — capturing a stack dump first."
dump_out=$(bash "$REPO/scripts/gaz_request.sh" gateway-dump 120 2>&1)
printf '%s\n' "$dump_out" >> "$LOG" 2>/dev/null

log "submitting gateway-restart via gaz_relay."
out=$(bash "$REPO/scripts/gaz_request.sh" gateway-restart 240 2>&1)
printf '%s\n' "$out" >> "$LOG" 2>/dev/null

rm -f "$FAILCT" 2>/dev/null

# Post-check, so a failed recovery is loud rather than silent.
sleep 10
recode=$(curl -s -m "$PROBE_TIMEOUT" -o /dev/null -w '%{http_code}' "$GW_URL" 2>/dev/null)
[ -z "$recode" ] && recode=000
if [ "$recode" != "000" ]; then
    log "gateway serving again (curl_code=$recode) — recovery OK."
else
    log "ERROR: gateway still not serving after restart (curl_code=$recode). Will retry after cooldown; needs manual attention."
fi
