#!/bin/bash
# WHG production autostart — invoked from gazetteer's @reboot crontab.
#
# Sequence: 1) wait for the /vast ES config to become readable, 2) activate the
# whg conda env, 3) start ES, then POLL until the cluster is ready (a cold-boot
# shard recovery can exceed es.sh's built-in 180s wait, which would otherwise
# cause Kibana + Gateway to be skipped), and only THEN start Kibana + Gateway.
# Every step is idempotent (es.sh subcommands are PID-guarded).
#
# Although version-controlled here in scripts/, the crontab invokes the copy in
# the /vast checkout so it still runs and logs even if /ix1 is slow/unavailable
# at boot (/vast is the resilient NFSv3 mount; /ix1 is NFSv4 and is what failed
# to mount after the May 2026 maintenance). Logs to /vast/ishi/es.
set -u
# ES runtime relocated to /vast (see developer/ix1-to-vast-es-migration-runbook.md):
# use the /vast clone's es.sh (which reads /vast paths from its .env.local) and the
# /vast password copy, so boot no longer waits on or depends on /ix1.
ESH=/vast/ishi/elastic/scripts
PW=/vast/ishi/es/config/elastic.password
LOG=/vast/ishi/es/boot_start_whg.log
exec >>"$LOG" 2>&1
echo "================ boot_start $(date '+%F %T %Z') ================"

# 1. Wait for the /vast ES config (password copy) to be readable (NFS can lag at boot).
ready=0
for i in $(seq 1 90); do                 # up to ~15 min
    if head -c1 "$PW" >/dev/null 2>&1; then ready=1; echo "[$(date '+%T')] /vast config ready after ~$((i*10))s"; break; fi
    sleep 10
done
if [ "$ready" -ne 1 ]; then
    echo "[$(date '+%T')] ERROR: /vast ES config not readable after 15 min - NOT starting (mount issue?)."
    exit 1
fi

# 2. Activate whg conda (gazetteer's LOCAL miniconda; @reboot has no login shell).
# conda's activate/deactivate hooks reference unbound vars (e.g. CONDA_BACKUP_CXX
# from the gxx compiler package), which are fatal under `set -u` — relax it here.
CONDA_SH=/home/gazetteer/miniconda/etc/profile.d/conda.sh
if [ -f "$CONDA_SH" ]; then
    set +u
    source "$CONDA_SH" && conda activate whg && echo "[$(date '+%T')] conda whg: $(which python)"
    set -u
else
    echo "[$(date '+%T')] WARNING: $CONDA_SH missing - gateway may fail to start."
fi

# 3. Start ES ONLY (don't rely on es -start's 180s wait, which skips Kibana/Gateway on slow boots).
echo "[$(date '+%T')] es es-start"
bash "$ESH/es.sh" es-start

# 4. Poll until ES cluster is green/yellow (cold-boot shard recovery can take minutes).
PASS=$(cat "$PW" 2>/dev/null)
es_ok=0
for i in $(seq 1 120); do                 # up to ~20 min
    st=$(curl -s -m 5 -u "elastic:${PASS}" "http://localhost:9201/_cluster/health" 2>/dev/null | grep -o '"status":"[a-z]*"')
    case "$st" in
        *green*|*yellow*) echo "[$(date '+%T')] ES ready: $st (after ~$((i*10))s)"; es_ok=1; break;;
    esac
    sleep 10
done
if [ "$es_ok" -ne 1 ]; then
    echo "[$(date '+%T')] ERROR: ES not green/yellow after 20 min - skipping Kibana/Gateway."
    exit 1
fi

# 5. ES is ready - start Kibana + Gateway (idempotent; PID-guarded).
echo "[$(date '+%T')] es kibana-start";  bash "$ESH/es.sh" kibana-start
# Gateway via gateway_ctl.sh (the /vast-aware manager) — es.sh's own gateway-start
# writes PID/logs under /ix1. `start` is a no-op if already running.
echo "[$(date '+%T')] gw start"; bash "$ESH/gateway_ctl.sh" start
sleep 3
echo "[$(date '+%T')] gateway health: $(curl -s -m 10 http://localhost:9200/api/health 2>/dev/null | head -c 200)"
echo "================ done $(date '+%F %T') ================"
