#!/bin/bash
# es_watchdog.sh — auto-recover production Elasticsearch if it dies while the box
# stays up (i.e. the @reboot autostart in boot_start_whg.sh never fires because
# there was no reboot — the exact gap that left prod ES down for hours after the
# 2026-07-20 system-wide OOM killed ES but not the VM).
#
# WHY a stg135-side watchdog (not a gazetteer cron):
#   ES/Kibana/gateway run as `gazetteer`, which cannot be SSH'd (sshd AllowUsers).
#   This watchdog only detects a down ES and runs the allowlisted `es-start` op
#   through scripts/gaz_run.sh, which executes it as gazetteer via stg135's
#   `sudo /bin/su - gazetteer` rule. (Until 2026-09-29 it went through the
#   cron-polled gaz_relay, now retired.)
#
# LIVENESS SIGNAL: any HTTP response from localhost:9201 (200 / 401 / 503 …) means
#   the ES process is listening = up. Only curl_code "000" (connection refused /
#   timeout) counts as down. No password needed, so a bad password can't misfire.
#
# ANTI-THRASH: requires FAIL_THRESHOLD consecutive down checks (ignores a GC-pause
#   blip), enforces a COOLDOWN between start attempts (a warm shard recovery can
#   take minutes — don't pile on), and skips if a relay request is already queued.
#
# MAINTENANCE: before an intentional `es-stop`, suppress the watchdog with
#       touch /vast/ishi/elastic/.es_watchdog.disabled
#   and remove that file to re-arm.
#
# INSTALL (as stg135, on pitt):
#   ( crontab -l 2>/dev/null | grep -v es_watchdog; \
#     echo '*/2 * * * * /vast/ishi/elastic/scripts/es_watchdog.sh' ) | crontab -
#
set -uo pipefail

REPO=/vast/ishi/elastic
ES_URL=http://localhost:9201/_cluster/health
STATE=$REPO/.es_watchdog                  # our own state (fail counter, last-start stamp)
LOG=$REPO/logs/es_watchdog.log
DISABLE_FLAG=$REPO/.es_watchdog.disabled

FAIL_THRESHOLD=2      # consecutive failing checks before acting
COOLDOWN=600          # seconds to wait after a start attempt before trying again

mkdir -p "$STATE" 2>/dev/null || true
FAILCT=$STATE/failcount
LASTSTART=$STATE/laststart

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG" 2>/dev/null; }

# Single-instance lock: a gaz_run op can block up to ~240s, longer than
# the 2-min cron interval — don't let runs stack. (No stderr redirect on this exec:
# it would silence the script's stderr for its whole remaining lifetime.)
exec 9>"$STATE/.lock" || exit 0
flock -n 9 || exit 0

# Maintenance suppression.
if [ -f "$DISABLE_FLAG" ]; then
    exit 0
fi

# --- liveness probe (no auth needed; any HTTP code == listening) ---
# curl's -w '%{http_code}' prints "000" on connection-refused/timeout and still
# writes it to stdout even though curl exits non-zero — so DON'T add `|| echo 000`
# (that would append a second 000 and make a down ES read as "up"). Just default
# an empty capture (e.g. curl missing) to 000.
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$ES_URL" 2>/dev/null)
[ -z "$code" ] && code=000

if [ "$code" != "000" ]; then
    # ES port is responding → healthy. Clear any accumulated failure state.
    [ -f "$FAILCT" ] && rm -f "$FAILCT" 2>/dev/null
    exit 0
fi

# --- ES port not responding ---
n=$(( $(cat "$FAILCT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAILCT"
log "ES unreachable on 9201 (curl_code=$code) — consecutive_failures=$n"

# Need sustained failure before acting (ride out transient blips).
if [ "$n" -lt "$FAIL_THRESHOLD" ]; then
    exit 0
fi

# Rate-limit: give a prior start attempt time to bring shards up.
now=$(date +%s)
last=$(cat "$LASTSTART" 2>/dev/null || echo 0)
if [ $(( now - last )) -lt "$COOLDOWN" ]; then
    log "within cooldown ($(( now - last ))s < ${COOLDOWN}s since last start) — waiting for ES to come up."
    exit 0
fi

# Act: run the allowlisted es-start op as gazetteer.
echo "$now" > "$LASTSTART"
log "ES down for $n consecutive checks — running es-start as gazetteer."
out=$(bash "$REPO/scripts/gaz_run.sh" es-start 240 2>&1)
log "gaz_run es-start result:"
printf '%s\n' "$out" >> "$LOG" 2>/dev/null

# Fresh failure accounting after an action.
rm -f "$FAILCT" 2>/dev/null

# Post-check so a failed recovery is loud in the log.
sleep 5
recode=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$ES_URL" 2>/dev/null)
[ -z "$recode" ] && recode=000
if [ "$recode" != "000" ]; then
    log "ES port responding again (curl_code=$recode) — recovery OK."
else
    log "ERROR: ES still unreachable after es-start (curl_code=$recode). Will retry after cooldown; may need manual attention (OOM loop?)."
fi
