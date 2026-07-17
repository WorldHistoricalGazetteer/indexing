#!/bin/bash
# Gateway control script for the WHG API gateway
# Usage: gateway_ctl.sh {start|stop|restart|status|pull}
#
# Must be run as the gazetteer user (owner of the gateway process).
# From an interactive SSH session: gateway_ctl.sh restart
# The es.sh alias can also be used for ES operations.
# Gateway runs from the /vast clone (ES + gateway relocated off /ix1, 2026-07-15).
GATEWAY_DIR="/vast/ishi/elastic"
LOGDIR="${GATEWAY_DIR}/logs"
LOGFILE="${LOGDIR}/gateway.log"
LOG_KEEP=5           # generations retained (gateway.log, .1 … .4)
_find_pid() {
    pgrep -f "python -m gateway" -u gazetteer 2>/dev/null | head -1
}
_rotate_log() {
    # Generational rotation on each (re)start. The gateway is low-volume
    # (uvicorn startup banner + occasional warnings/tracebacks), so rotating
    # when a new process takes over — and keeping the last $LOG_KEEP files —
    # bounds disk without a logrotate dependency and keeps each run's output
    # (esp. a startup crash) in its own file. Was /dev/null, which left real
    # failures untraceable.
    mkdir -p "$LOGDIR" 2>/dev/null || true
    [[ -f "$LOGFILE" ]] || return 0
    local i
    for (( i=LOG_KEEP-1; i>=1; i-- )); do
        [[ -f "$LOGFILE.$i" ]] && mv -f "$LOGFILE.$i" "$LOGFILE.$((i+1))"
    done
    mv -f "$LOGFILE" "$LOGFILE.1"
}
do_status() {
    local pid=$(_find_pid)
    if [[ -n "$pid" ]]; then
        echo "Gateway running (PID $pid)"
        return 0
    else
        echo "Gateway is NOT running"
        return 1
    fi
}
do_stop() {
    local pid=$(_find_pid)
    if [[ -z "$pid" ]]; then
        echo "Gateway is not running"
        return 0
    fi
    echo "Stopping gateway (PID $pid)..."
    kill "$pid" 2>/dev/null
    sleep 2
    # Force kill if still running
    if kill -0 "$pid" 2>/dev/null; then
        echo "Force killing..."
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi
    echo "Stopped"
}
do_pull() {
    echo "Pulling latest gateway code..."
    # --ff-only: never auto-merge or open an editor on divergence (which would
    # hang this non-interactive script); fail cleanly instead.
    # -c safe.directory: the /vast clone is owned by stg135 but gw runs as
    # gazetteer, so git's dubious-ownership guard would otherwise refuse the pull.
    git -C "$GATEWAY_DIR" -c "safe.directory=$GATEWAY_DIR" pull --ff-only origin main
}
_activate_conda() {
    # gateway_ctl may be invoked from a bare environment where the whg conda env
    # is NOT active — notably the cron-driven gaz_relay (which reaches this script
    # with cron's minimal PATH, so `python` is system python3.9 and `python -m
    # gateway` dies on import). Interactive `gw` and the @reboot boot script work
    # only because THEY activate whg first. Activate gazetteer's LOCAL miniconda
    # here so `do_start` is self-sufficient regardless of caller env. conda's
    # activate hooks reference unbound vars (e.g. CONDA_BACKUP_CXX), so relax
    # `set -u` around the source and restore the prior state afterwards.
    local CONDA_SH=/home/gazetteer/miniconda/etc/profile.d/conda.sh
    if [[ -f "$CONDA_SH" ]]; then
        local _had_u=0; [[ $- == *u* ]] && _had_u=1
        set +u
        # shellcheck disable=SC1090
        source "$CONDA_SH" && conda activate whg
        (( _had_u )) && set -u
    else
        echo "WARNING: $CONDA_SH missing — gateway may fail to start." >&2
    fi
}
do_start() {
    local pid=$(_find_pid)
    if [[ -n "$pid" ]]; then
        echo "Gateway already running (PID $pid)"
        return 1
    fi
    echo "Starting gateway..."
    _activate_conda
    _rotate_log
    cd "$GATEWAY_DIR"
    echo "Logging to $LOGFILE"
    nohup python -m gateway >> "$LOGFILE" 2>&1 &
    sleep 3
    pid=$(_find_pid)
    if [[ -n "$pid" ]]; then
        echo "Gateway started (PID $pid)"
    else
        echo "FAILED to start gateway"
        return 1
    fi
}
do_restart() {
    # Pull FIRST, while the gateway keeps serving on its old (in-memory) code —
    # `git pull` doesn't affect a running process. The pull is best-effort: since
    # `--ff-only` is atomic (a failed pull leaves the clone at its previous, working
    # commit), a pull failure (offline, no git auth, etc.) is NON-fatal — we warn and
    # restart on the current code rather than strand the operator unable to restart.
    if ! do_pull; then
        echo "WARNING: pull failed (offline / no git auth / ownership) — restarting on CURRENT code." >&2
    fi
    do_stop
    do_start
}
case "${1:-status}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    pull)    do_pull    ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|pull}"
        exit 1
        ;;
esac
