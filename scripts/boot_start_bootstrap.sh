#!/bin/bash
# WHG autostart BOOTSTRAP — the @reboot entry point on the gazetteer VM.
#
# Why this exists: cron's @reboot can fire before the NFS mounts are ready, so
# a wrapper living on /vast can be missing at that instant and the job silently
# no-ops (observed after the 2026-05-20 maintenance reboot — no log produced).
#
# This shim is deployed to LOCAL disk (/home/gazetteer/bin/) so it is always
# present at boot. It waits for the /vast checkout to appear, then hands off to
# the version-controlled wrapper (scripts/boot_start_whg.sh) — keeping the
# actual boot logic single-source in git, auto-updated via `es -update`.
#
# Deploy: cp this to /home/gazetteer/bin/boot_start_bootstrap.sh (chmod +x) and
# point gazetteer's crontab at it:
#   @reboot /home/gazetteer/bin/boot_start_bootstrap.sh
TARGET=/vast/ishi/elastic/scripts/boot_start_whg.sh
LOG=/home/gazetteer/boot_start_bootstrap.log     # local — works even if NFS is down
echo "[$(date '+%F %T %Z')] bootstrap: waiting for $TARGET" >>"$LOG"
for i in $(seq 1 60); do                          # up to ~10 min for /vast to mount
    if [ -x "$TARGET" ]; then
        echo "[$(date '+%T')] /vast ready after ~$((i*10))s — handing off" >>"$LOG"
        exec "$TARGET"
    fi
    sleep 10
done
echo "[$(date '+%T')] ERROR: $TARGET never appeared (/vast not mounted?) — services NOT started" >>"$LOG"
exit 1
