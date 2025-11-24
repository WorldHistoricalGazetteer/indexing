#!/bin/bash
# ~/es
# Single wrapper for Elasticsearch and Kibana

ES_BIN="/ix1/whcdh/es-bin/bin/elasticsearch"
ES_DATA="/ix1/whcdh/es/data"
ES_LOGS="/ix1/whcdh/es/logs"
ES_REPO="/ix1/whcdh/es/repo"

KB_BIN="/ix1/whcdh/kibana-bin/bin/kibana"
KB_DATA="/ix1/whcdh/kibana/data"
KB_LOGS="/ix1/whcdh/kibana/logs/kibana.log"

ES_PID_FILE="/ix1/whcdh/es/es.pid"
KB_PID_FILE="/ix1/whcdh/kibana/kb.pid"

start_es() {
    if [ -f "$ES_PID_FILE" ] && kill -0 $(cat "$ES_PID_FILE") 2>/dev/null; then
        echo "Elasticsearch already running."
        return
    fi
    echo "Starting Elasticsearch..."
    nohup $ES_BIN \
        -E path.data="$ES_DATA" \
        -E path.logs="$ES_LOGS" \
        -E path.repo="$ES_REPO" \
        -E discovery.type=single-node \
        -E xpack.security.enabled=false \
        -E network.host=0.0.0.0 \
        > "$ES_LOGS/nohup.out" 2>&1 &
    echo $! > "$ES_PID_FILE"
}

stop_es() {
    if [ -f "$ES_PID_FILE" ]; then
        kill -9 $(cat "$ES_PID_FILE") 2>/dev/null && rm -f "$ES_PID_FILE"
        echo "Elasticsearch stopped."
    else
        echo "Elasticsearch is not running."
    fi
}

start_kb() {
    if [ -f "$KB_PID_FILE" ] && kill -0 $(cat "$KB_PID_FILE") 2>/dev/null; then
        echo "Kibana already running."
        return
    fi
    echo "Waiting for Elasticsearch to be ready..."
    while ! curl -s http://localhost:9200 >/dev/null 2>&1; do
        sleep 2
    done

    echo "Starting Kibana..."
    nohup /ix1/whcdh/kibana-bin/bin/kibana \
          --path.data=/ix1/whcdh/kibana/data \
            > /dev/null 2>&1 &
    echo $! > "$KB_PID_FILE"
}

stop_kb() {
    if [ -f "$KB_PID_FILE" ]; then
        kill -9 $(cat "$KB_PID_FILE") 2>/dev/null && rm -f "$KB_PID_FILE"
        echo "Kibana stopped."
    else
        echo "Kibana is not running."
    fi
}

case "$1" in
    -start)
        start_es
        start_kb
        ;;
    -stop)
        stop_kb
        stop_es
        ;;
    -restart)
        stop_kb
        stop_es
        start_es
        start_kb
        ;;
    es-start)
        start_es
        ;;
    es-stop)
        stop_es
        ;;
    es-restart)
        stop_es
        start_es
        ;;
    kibana-start)
        start_kb
        ;;
    kibana-stop)
        stop_kb
        ;;
    kibana-restart)
        stop_kb
        start_kb
        ;;
    *)
        echo "Usage: $0 {-start|-stop|-restart|es-start|es-stop|es-restart|kibana-start|kibana-stop|kibana-restart}"
        ;;
esac