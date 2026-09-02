#!/bin/bash
# place#233 — volume capacity guards.
#
# TWO volumes, two very different risk profiles:
#
#   /vast/ishi   1024 GB, 226 GB free — SHARED WITH PRODUCTION ELASTICSEARCH.
#                Filling it takes the indexes read-only; that has happened in
#                this project before. We avoid it entirely and guard it anyway.
#   /ix1/ishi    5120 GB, 1837 GB free — no ES. Bulk work lives here.
#
# ⚠️ MEASURE THE QUOTA, NOT THE POOL. This is the trap that made the first
# version of this guard decorative. Both readings print "Mounted on /vast":
#     df -h /vast       -> 3.9P total, 3.3P avail   (whole VAST pool)
#     df -h /vast/ishi  -> 1.0T total, 226G avail   (our project quota)
# A guard pointed at /vast compares 3.3 PB against a 160 GB floor and can
# NEVER fire. Always stat a path INSIDE the quota.
#
# ES disk watermarks on the 1024 GB /vast volume, which is where the 160 GB
# floor comes from (it sits at the LOW watermark plus margin, so this campaign
# never puts ES into even its first degraded state):
#     low   85% -> free < 153.6 GB   ES stops allocating shards
#     high  90% -> free < 102.4 GB   ES relocates shards away
#     flood 95% -> free <  51.2 GB   INDICES GO READ-ONLY   <- the outage

VAST_PATH="${VAST_PATH:-/vast/ishi}"
VAST_FLOOR_GB="${VAST_FLOOR_GB:-160}"
IX1_PATH="${IX1_PATH:-/ix1/ishi}"
IX1_FLOOR_GB="${IX1_FLOOR_GB:-400}"

vol_avail_gb() {
    df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9'
}

# guard_volume <label> <path> <floor_gb> [expected_write_gb]
guard_volume() {
    local label="$1" path="$2" floor_gb="$3" need_gb="${4:-0}"
    local avail_gb
    avail_gb=$(vol_avail_gb "$path")
    if [ -z "$avail_gb" ]; then
        echo "[guard:${label}] FATAL: cannot read free space on ${path}" >&2
        exit 90
    fi
    echo "[guard:${label}] path=${path} free=${avail_gb}G floor=${floor_gb}G expected_write=${need_gb}G"
    if [ "$avail_gb" -lt "$floor_gb" ]; then
        echo "[guard:${label}] FATAL: free ${avail_gb}G below floor ${floor_gb}G — refusing to start" >&2
        exit 90
    fi
    if [ "$need_gb" -gt 0 ] && [ $(( avail_gb - need_gb )) -lt "$floor_gb" ]; then
        echo "[guard:${label}] FATAL: writing ~${need_gb}G would leave $(( avail_gb - need_gb ))G, below floor ${floor_gb}G" >&2
        exit 91
    fi
    echo "[guard:${label}] OK"
}

# guard_selftest <path>
# Proves the guard DISCRIMINATES rather than merely runs. A check that cannot
# fail is decorative, and this one silently could not before the quota fix.
# So run it against a known-bad input and require it to abort.
guard_selftest() {
    local path="${1:-$VAST_PATH}"
    echo "[selftest] pool vs quota (these MUST differ, both say 'Mounted on /vast'):"
    echo -n "[selftest]   /vast  avail: "; df -BG --output=avail /vast 2>/dev/null | tail -1
    echo -n "[selftest]   ${path} avail: "; df -BG --output=avail "$path" 2>/dev/null | tail -1
    echo "[selftest] invoking guard with an impossible floor (999999G); it MUST abort:"
    ( guard_volume selftest "$path" 999999 0 ) && {
        echo "[selftest] FATAL: guard did NOT fire on known-bad input — it is decorative. Refusing." >&2
        exit 94
    }
    echo "[selftest] PASS: guard aborted on known-bad input, so it can fire."
}

# watchdog_volume <label> <path> <floor_gb> [interval_s]
watchdog_volume() {
    local label="$1" path="$2" floor_gb="$3" interval="${4:-120}"
    (
        while true; do
            sleep "$interval"
            local a; a=$(vol_avail_gb "$path")
            if [ -n "$a" ] && [ "$a" -lt "$floor_gb" ]; then
                echo "[watchdog:${label}] FATAL: free ${a}G fell below floor ${floor_gb}G — cancelling ${SLURM_JOB_ID}" >&2
                scancel "${SLURM_JOB_ID}"
                exit 92
            fi
            echo "[watchdog:${label}] free=${a}G floor=${floor_gb}G $(date -Is)"
        done
    ) &
    WATCHDOG_PIDS="${WATCHDOG_PIDS} $!"
    echo "[watchdog:${label}] armed pid=$! path=${path} floor=${floor_gb}G"
}

watchdogs_stop() {
    for p in ${WATCHDOG_PIDS}; do kill "$p" 2>/dev/null || true; done
    echo "[watchdog] all stopped"
}

report_volumes() {
    echo "[report] $(date -Is)"
    printf '  %-14s ' "/vast/ishi:"; df -h "${VAST_PATH}" | tail -1
    printf '  %-14s ' "/ix1/ishi:";  df -h "${IX1_PATH}"  | tail -1
    [ -n "${SLURM_SCRATCH}" ] && { printf '  %-14s ' "scratch:"; df -h "${SLURM_SCRATCH}" 2>/dev/null | tail -1; }
    if [ -n "${RUN_ROOT}" ] && [ -d "${RUN_ROOT}" ]; then
        echo "[report] campaign footprint:"; du -sh "${RUN_ROOT}" 2>/dev/null
        du -sh "${RUN_ROOT}"/* 2>/dev/null || true
    fi
}
