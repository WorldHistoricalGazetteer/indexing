#!/bin/bash
# ~/es
# Single wrapper for Elasticsearch and Kibana

# --- Load Environment Variables ---
ENV_FILE="/ix1/whcdh/elastic/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "ERROR: .env file not found at $ENV_FILE"
    exit 1
fi

start_es() {
    if [ -f "$ES_PID_FILE" ] && kill -0 $(cat "$ES_PID_FILE") 2>/dev/null; then
        echo "Elasticsearch already running."
        return
    fi
    echo "Starting Elasticsearch..."

    # Use the variables from the .env file
    nohup $ES_BIN_PATH \
        -E cluster.name="$ES_CLUSTER_NAME" \
        -E node.name="$ES_NODE_NAME" \
        -E path.data="$ES_DATA_PATH" \
        -E path.logs="$ES_LOGS_PATH" \
        -E path.repo="$ES_REPO_ROOT" \
        -E discovery.type=single-node \
        -E xpack.security.enabled=false \
        -E network.host="$ES_HOST_NAME" \
        -E http.port="$ES_HOST_PORT" \
        > "$ES_LOGS_PATH/nohup.out" 2>&1 &
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
    while ! curl -s "$ES_HOST_URL" >/dev/null 2>&1; do
        sleep 2
    done

    echo "Starting Kibana..."
    nohup "$KB_BIN_PATH" \
          --path.data="${NFS_MOUNT}/kibana/data" \
            > /dev/null 2>&1 &
    echo $! > "$KB_PID_FILE"
}

launch_staging() {
    STAGING_SCRIPT="${NFS_MOUNT}/elastic/processing/es_staging.sbatch"

    echo "Launching staging Elasticsearch..."
    JOBID=$(sbatch --parsable "$STAGING_SCRIPT")
    echo "Launched staging ES as job $JOBID"
    squeue -j "$JOBID"

    INFO="${NFS_MOUNT}/esinfo/es-$JOBID.env"

    echo -n "Waiting for ES info file..."
    while [ ! -f "$INFO" ]; do
        sleep 2
    done
    echo " ready."

    source "$INFO"

    export JOBID ES_NODE ES_PORT ES_DATA
    echo "ES Node: $ES_NODE"
    echo "ES Port: $ES_PORT"
    echo "ES Data Dir: $ES_DATA"
    echo "ES Env File: $INFO"
}

down_staging() {
    if [ -z "$JOBID" ]; then
        echo "ERROR: No staging JOBID exported in environment."
        echo "If you lost it, inspect ${NFS_MOUNT}/esinfo/"
        return 1
    fi

    echo "Stopping staging ES job $JOBID..."
    scancel "$JOBID"

    INFO="${NFS_MOUNT}/esinfo/es-$JOBID.env"
    rm -f "$INFO"

    unset ES_NODE ES_PORT ES_DATA JOBID

    echo "Staging instance stopped and cleaned."
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
    -staging-start)
        launch_staging
        ;;
    -staging-stop)
        down_staging
        ;;
    *)
        echo "Usage: $0 OPTIONS"
        echo
        echo " Local VM services:"
        echo "   -start            Start Elasticsearch + Kibana"
        echo "   -stop             Stop Elasticsearch + Kibana"
        echo "   -restart          Restart both"
        echo "   es-start          Start Elasticsearch only"
        echo "   es-stop           Stop Elasticsearch only"
        echo "   es-restart        Restart Elasticsearch only"
        echo "   kibana-start      Start Kibana only"
        echo "   kibana-stop       Stop Kibana only"
        echo "   kibana-restart    Restart Kibana only"
        echo
        echo " Staging ES via Slurm (use on login node and include full path):"
        echo "   source ${NFS_MOUNT}/elastic/scripts/es.sh -staging-start    Launch staging Elasticsearch instance"
        echo "   source ${NFS_MOUNT}/elastic/scripts/es.sh -staging-stop     Stop staging ES (requires JOBID exported)"
        echo
        echo "Notes:"
        echo " * After -staging, variables ES_NODE, ES_PORT, ES_DATA, JOBID"
        echo "   are exported into this shell."
        echo " * Connect from your local machine via:"
        echo "       ssh -L ${ES_HOST_PORT}:${ES_HOST_NAME}:\$ES_PORT ${USER}@gazetteer.crcd.pitt.edu"
        ;;
esac