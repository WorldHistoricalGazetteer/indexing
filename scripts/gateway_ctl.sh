#!/bin/bash
# Gateway control script for the WHG API gateway
# Usage: gateway_ctl.sh {start|stop|restart|status|pull}
#
# Must be run as the gazetteer user (owner of the gateway process).
# From an interactive SSH session: gateway_ctl.sh restart
# The es.sh alias can also be used for ES operations.
GATEWAY_DIR="/ix1/ishi/elastic"
LOGFILE="/dev/null"
_find_pid() {
    pgrep -f "python -m gateway" -u gazetteer 2>/dev/null | head -1
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
    cd "$GATEWAY_DIR" && git pull origin main
}
do_start() {
    local pid=$(_find_pid)
    if [[ -n "$pid" ]]; then
        echo "Gateway already running (PID $pid)"
        return 1
    fi
    echo "Starting gateway..."
    cd "$GATEWAY_DIR"
    nohup python -m gateway > "$LOGFILE" 2>&1 &
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
    do_stop
    do_pull
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
