#!/bin/bash
# Gateway process manager
# Usage: gateway.sh [start|stop|restart|status]

GATEWAY_DIR="/ix1/ishi/elastic"
PIDFILE="$GATEWAY_DIR/gateway.pid"
LOGFILE="$GATEWAY_DIR/logs/gateway.log"

mkdir -p "$GATEWAY_DIR/logs"

get_pid() {
    # Find running gateway process
    pgrep -f "python -m gateway" 2>/dev/null
}

do_status() {
    pid=$(get_pid)
    if [ -n "$pid" ]; then
        echo "Gateway is running (PID $pid)"
        return 0
    else
        echo "Gateway is not running"
        return 1
    fi
}

do_stop() {
    pid=$(get_pid)
    if [ -n "$pid" ]; then
        echo "Stopping gateway (PID $pid)..."
        kill "$pid"
        sleep 1
        # Verify it stopped
        if get_pid > /dev/null; then
            echo "Process still running, sending SIGKILL..."
            kill -9 "$pid"
            sleep 1
        fi
        echo "Gateway stopped."
    else
        echo "Gateway is not running."
    fi
}

do_start() {
    if get_pid > /dev/null; then
        echo "Gateway is already running (PID $(get_pid)). Use restart instead."
        return 1
    fi
    echo "Starting gateway..."
    cd "$GATEWAY_DIR"
    nohup python -m gateway >> "$LOGFILE" 2>&1 &
    new_pid=$!
    echo "$new_pid" > "$PIDFILE"
    sleep 2
    if get_pid > /dev/null; then
        echo "Gateway started (PID $new_pid), logging to $LOGFILE"
    else
        echo "ERROR: Gateway failed to start. Check $LOGFILE"
        tail -5 "$LOGFILE"
        return 1
    fi
}

do_restart() {
    do_stop
    do_start
}

case "${1:-status}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    status)  do_status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" ;;
esac

