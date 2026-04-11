#!/bin/bash
# =============================================================================
# scripts/cluster.sh
# Place clustering orchestration (entity resolution across authorities)
# =============================================================================
# Sourced by es.sh — not intended for standalone execution.
#
# Functions:
#   do_cluster            Run the WHG place clustering pipeline
#   do_cluster_finalize   Restore cluster results to production (alias swap)
#
# Internal helpers:
#   _ensure_exchange_repo  Register snapshot exchange repo on an ES instance
#   _resolve_aliases       Resolve alias names to concrete backing indices
#   _take_snapshot         Snapshot specific indices (async + poll)
#   _restore_snapshot      Restore a snapshot (non-blocking)
#   _wait_for_restore      Block until all shard recovery finishes

source "${BASH_SOURCE[0]%/*}/_common.sh"

# =============================================================================
# CLUSTER EXCHANGE HELPERS (snapshot handoff between prod ↔ staging)
# =============================================================================

# Register the cluster_exchange snapshot repo on a given ES instance.
_ensure_exchange_repo() {
    local es_url="$1"          # e.g. http://localhost:9201
    local curl_cmd="${2:-curl}" # "es_curl" for production, "curl" for staging

    mkdir -p "${CLUSTER_EXCHANGE_DIR}"
    local resp
    resp=$($curl_cmd -s -X PUT "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}" \
        -H 'Content-Type: application/json' -d "{
      \"type\": \"fs\",
      \"settings\": { \"location\": \"${CLUSTER_EXCHANGE_DIR}\" }
    }" 2>&1)
    if echo "$resp" | grep -q '"acknowledged"' 2>/dev/null; then
        return 0
    fi
    echo "  ERROR registering exchange repo on ${es_url}:" >&2
    echo "  $resp" >&2
    return 1
}

# Resolve alias names to their concrete backing indices.
# Outputs a comma-separated list of concrete index names.
# Any name that is already a concrete index is passed through unchanged.
_resolve_aliases() {
    local es_url="$1"
    local aliases="$2"          # comma-separated alias (or index) names
    local curl_cmd="${3:-curl}"

    $curl_cmd -s "${es_url}/_alias/${aliases}" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(','.join(sorted(data.keys())))
except:
    # If _alias endpoint fails, fall back to the original names
    print('${aliases}')
"
}

# Take a snapshot of specific indices. Starts async, then polls until done.
# (Avoids gateway read-timeout on long-running snapshots.)
_take_snapshot() {
    local es_url="$1"
    local snap_name="$2"
    local indices="$3"         # comma-separated
    local curl_cmd="${4:-curl}"

    echo "  Snapshotting ${indices} → ${CLUSTER_EXCHANGE_REPO}/${snap_name} ..."
    echo "  (this may take several minutes for large indices)"

    # Start snapshot asynchronously (no wait_for_completion)
    local resp
    resp=$($curl_cmd -s -X PUT \
        "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}" \
        -H 'Content-Type: application/json' -d "{
      \"indices\": \"${indices}\",
      \"ignore_unavailable\": true,
      \"include_global_state\": false
    }" 2>&1)
    if ! echo "$resp" | grep -q '"accepted"' 2>/dev/null; then
        echo "  ERROR: Snapshot request rejected:" >&2
        echo "  $resp" | head -20 >&2
        return 1
    fi

    # Poll until snapshot completes
    echo -n "  Waiting ..."
    while true; do
        sleep 10
        local state
        state=$($curl_cmd -s \
            "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}" 2>/dev/null \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['snapshots'][0]['state'])" 2>/dev/null || echo "UNKNOWN")
        case "$state" in
            SUCCESS)
                echo " done"
                echo "  ✓ Snapshot complete"
                return 0
                ;;
            FAILED|PARTIAL|MISSING|UNKNOWN)
                echo " $state"
                echo "  ERROR: Snapshot ended in state: $state" >&2
                $curl_cmd -s "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}" 2>/dev/null \
                    | python3 -m json.tool 2>/dev/null | head -30 >&2
                return 1
                ;;
            *)  # IN_PROGRESS, STARTED, etc.
                echo -n "."
                ;;
        esac
    done
}

# Restore a snapshot. Does NOT block; caller should poll _wait_for_restore.
_restore_snapshot() {
    local es_url="$1"
    local snap_name="$2"
    local indices="$3"         # comma-separated (empty = all in snapshot)
    local curl_cmd="${4:-curl}"
    local extra_body="${5:-}"   # extra JSON fields (rename_pattern etc.)

    local body="{
      \"ignore_unavailable\": true,
      \"include_global_state\": false"
    [ -n "$indices" ] && body+=", \"indices\": \"${indices}\""
    [ -n "$extra_body" ] && body+=", ${extra_body}"
    body+="}"

    echo "  Restoring ${CLUSTER_EXCHANGE_REPO}/${snap_name} ..."
    local resp
    resp=$($curl_cmd -s -X POST \
        "${es_url}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${snap_name}/_restore" \
        -H 'Content-Type: application/json' -d "$body" 2>&1)
    if echo "$resp" | grep -q '"accepted"' 2>/dev/null; then
        return 0
    fi
    echo "  ERROR: Restore failed:" >&2
    echo "  $resp" | head -20 >&2
    return 1
}

# Block until all shard recovery on an ES instance finishes.
_wait_for_restore() {
    local es_url="$1"
    local curl_cmd="${2:-curl}"

    echo -n "  Waiting for restore to complete"
    while true; do
        local recovering
        recovering=$($curl_cmd -s "${es_url}/_cat/recovery?active_only=true" 2>/dev/null | wc -l)
        [ "$recovering" -eq 0 ] && break
        echo -n "."
        sleep 5
    done
    echo " done"
}

# =============================================================================
# PLACE CLUSTERING
# =============================================================================

do_cluster() {
    # Usage: es -cluster [OPTIONS]
    #
    # Run the WHG place clustering pipeline: entity resolution across
    # authority records via hard links, toponym co-attestation, and
    # Symphonym phonetic similarity.
    #
    # Two execution modes:
    #   (a) Default (on Pitt VM): runs under nohup against localhost ES.
    #   (b) --slurm (from CRC login node): snapshots production indices
    #       into the staging ES on Slurm, runs clustering there, then
    #       use  es -cluster-finalize  to push results back to production.
    #
    # Options are passed through to clustering.runner (see --help).
    # ES host and password are injected automatically.
    #
    # Examples:
    #   es -cluster --full                  # nohup on VM
    #   es -cluster --full --slurm          # snapshot → staging → Slurm job
    #   es -cluster --incremental           # nohup on VM
    #   es -cluster --full --dry-run        # nohup, no indexing
    #   es -cluster --stats                 # quick query (no nohup)
    #   es -cluster --full --slurm --mem 300G --time 24:00:00

    # --- Parse our flags (--slurm, --mem, --time); rest goes to Python ---
    local USE_SLURM=false
    local SLURM_MEM="500G"
    local SLURM_TIME="3-00:00:00"
    local PYTHON_ARGS=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --slurm)    USE_SLURM=true; shift ;;
            --mem)      SLURM_MEM="$2"; shift 2 ;;
            --time)     SLURM_TIME="$2"; shift 2 ;;
            *)          PYTHON_ARGS+=("$1"); shift ;;
        esac
    done

    # --resume implies --full (it resumes a previous full run).
    # Add --full automatically if no mode flag was given.
    local HAS_MODE=false
    for _a in "${PYTHON_ARGS[@]}"; do
        case "$_a" in --full|--incremental|--stats) HAS_MODE=true ;; esac
    done
    if ! $HAS_MODE; then
        # Default to --full; if --resume is present the user clearly
        # wants to continue a full run.
        PYTHON_ARGS=("--full" "${PYTHON_ARGS[@]}")
    fi

    local PASS_FILE="${IX1_BASE}/es/config/elastic.password"
    local LOG_DIR="${IX1_BASE}/es/logs"
    local TIMESTAMP
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)

    mkdir -p "$LOG_DIR"

    # Build the full python command as a flat string — avoids heredoc
    # quoting pitfalls with arrays and line-continuations.
    local PYTHON_CMD="python -u -m clustering.runner"

    # ---- Slurm mode: snapshot prod → staging, run on Slurm ----
    if $USE_SLURM; then
        # From a CRC login node, production ES is reachable via the gateway
        # (localhost:9201 is only valid on the VM itself).
        local PROD_URL="http://gazetteer.crcd.pitt.edu:${GATEWAY_PORT:-9200}"
        local SNAP_NAME="cluster_input_${TIMESTAMP}"

        echo "=========================================="
        echo "PLACE CLUSTERING (Slurm via staging ES)"
        echo "=========================================="
        echo "Args:     ${PYTHON_ARGS[*]}"
        echo "Mem:      ${SLURM_MEM}"
        echo "Time:     ${SLURM_TIME}"
        echo

        # 1. Verify production ES is reachable (from login node / VM)
        echo "Step 1: Checking production ES at ${PROD_URL} ..."
        if ! es_curl --connect-timeout 10 "${PROD_URL}/_cluster/health" &>/dev/null; then
            echo "ERROR: Cannot reach production ES at ${PROD_URL}"
            return 1
        fi
        echo "  ✓ Production ES reachable"
        echo

        # 2. Ensure staging ES is running (auto-start if needed)
        echo "Step 2: Checking staging ES ..."
        local NEED_STAGING=false
        if [ ! -f "$STAGING_INFO_FILE" ]; then
            NEED_STAGING=true
        else
            source "$STAGING_INFO_FILE"
            if ! curl -sf --connect-timeout 10 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
                echo "  Staging ES at http://${ES_NODE}:${ES_PORT} is not responding (job may have expired)."
                NEED_STAGING=true
            fi
        fi
        if $NEED_STAGING; then
            echo "  No running staging ES found — starting one automatically ..."
            staging_start
            if [ ! -f "$STAGING_INFO_FILE" ]; then
                echo "  ERROR: Failed to auto-start staging ES."
                echo "  Try manually:  es -staging-start"
                return 1
            fi
            source "$STAGING_INFO_FILE"
        fi
        local STAGING_URL="http://${ES_NODE}:${ES_PORT}"
        if ! curl -sf --connect-timeout 10 "${STAGING_URL}/_cluster/health" &>/dev/null; then
            echo "  ERROR: Staging ES at ${STAGING_URL} is still not responding after auto-start."
            return 1
        fi
        echo "  ✓ Staging ES reachable at ${STAGING_URL}"
        echo

        # Check whether we can skip snapshot/restore (--resume with data already on staging)
        local SKIP_SNAPSHOT=false
        if printf '%s\n' "${PYTHON_ARGS[@]}" | grep -qx -- '--resume'; then
            if curl -sf "${STAGING_URL}/places/_count" >/dev/null 2>&1; then
                SKIP_SNAPSHOT=true
                echo "  --resume: places index already on staging, skipping snapshot/restore"
                echo "  Index status on staging:"
                curl -sf "${STAGING_URL}/_cat/indices?v&h=index,docs.count,store.size" 2>/dev/null || true
                echo
            fi
        fi

        if ! $SKIP_SNAPSHOT; then

        # 3. Register exchange repo on both instances
        echo "Step 3: Registering snapshot exchange repo ..."
        _ensure_exchange_repo "$PROD_URL" es_curl
        echo "  ✓ Production"
        _ensure_exchange_repo "$STAGING_URL" curl
        echo "  ✓ Staging"
        echo

        # 4. Resolve aliases and snapshot production input indices
        echo "Step 4: Snapshotting production indices ..."
        # 'places' and 'toponyms' are aliases pointing to versioned concrete
        # indices (e.g. places_20250320).  The snapshot API requires concrete
        # index names — alias names are silently ignored.
        local CONCRETE_INDICES
        CONCRETE_INDICES=$(_resolve_aliases "$PROD_URL" "places,toponyms" es_curl)
        echo "  Resolved aliases → concrete indices: ${CONCRETE_INDICES}"
        if [ -z "$CONCRETE_INDICES" ] || [ "$CONCRETE_INDICES" = "places,toponyms" ]; then
            # Fallback: if _alias returned the same names, try a wildcard approach
            CONCRETE_INDICES=$(es_curl -s "${PROD_URL}/_cat/indices/places*,toponyms*?h=index" 2>/dev/null \
                | tr '\n' ',' | sed 's/,$//')
            echo "  (wildcard fallback: ${CONCRETE_INDICES})"
        fi
        if [ -z "$CONCRETE_INDICES" ]; then
            echo "  ERROR: Could not resolve any concrete indices for places,toponyms" >&2
            return 1
        fi
        _take_snapshot "$PROD_URL" "$SNAP_NAME" "$CONCRETE_INDICES" es_curl
        echo

        # 5. Delete any pre-existing copies on staging, then restore
        echo "Step 5: Restoring input indices into staging ES ..."
        # Delete both aliases and concrete indices that might exist
        for idx in places toponyms $(echo "$CONCRETE_INDICES" | tr ',' ' '); do
            curl -s -X DELETE "${STAGING_URL}/${idx}" >/dev/null 2>&1 || true
        done
        # Force staging ES to rediscover snapshots: DELETE the repo
        # registration then re-register.  A simple PUT re-registration
        # is not sufficient — ES may serve its cached repo metadata
        # without re-reading the shared filesystem.
        curl -s -X DELETE "${STAGING_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}" >/dev/null 2>&1 || true
        _ensure_exchange_repo "$STAGING_URL" curl
        _restore_snapshot "$STAGING_URL" "$SNAP_NAME" "$CONCRETE_INDICES" curl
        _wait_for_restore "$STAGING_URL" curl

        # Staging is single-node so replica shards can never be assigned.
        # Set replicas to 0 on the restored indices to avoid a permanently
        # yellow cluster that never reaches green.
        echo "  Setting number_of_replicas=0 on restored indices ..."
        for idx in $(echo "$CONCRETE_INDICES" | tr ',' ' '); do
            curl -s -X PUT "${STAGING_URL}/${idx}/_settings" \
                -H 'Content-Type: application/json' \
                -d '{"index":{"number_of_replicas":0}}' >/dev/null 2>&1 || true
        done

        # Wait for cluster health to reach green (all shards assigned).
        # With replicas=0 this means only primaries need to finish initialising.
        echo -n "  Waiting for index shards to initialise ..."
        for _i in $(seq 1 360); do  # up to 60 minutes
            local HEALTH
            HEALTH=$(curl -sf "${STAGING_URL}/_cluster/health" 2>/dev/null \
                | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','red'))" 2>/dev/null || echo "red")
            if [ "$HEALTH" = "green" ]; then
                break
            fi
            echo -n "."
            sleep 10
        done
        echo " done"

        # Re-create aliases on staging so the clustering code can find
        # the indices by their canonical names (places, toponyms)
        echo "  Creating aliases on staging ..."
        for ALIAS_NAME in places toponyms; do
            # Find the concrete index matching this alias prefix
            local CONCRETE
            CONCRETE=$(echo "$CONCRETE_INDICES" | tr ',' '\n' \
                | grep -E "^${ALIAS_NAME}(_|$)" | head -1)
            if [ -n "$CONCRETE" ] && [ "$CONCRETE" != "$ALIAS_NAME" ]; then
                curl -s -X POST "${STAGING_URL}/_aliases" \
                    -H 'Content-Type: application/json' -d "{
                  \"actions\": [{\"add\": {\"index\": \"${CONCRETE}\", \"alias\": \"${ALIAS_NAME}\"}}]
                }" >/dev/null 2>&1
                echo "    ${ALIAS_NAME} → ${CONCRETE}"
            fi
        done
        echo "  Index status on staging:"
        curl -sf "${STAGING_URL}/_cat/indices?v&h=index,docs.count,store.size" 2>/dev/null || true
        echo

        fi  # end SKIP_SNAPSHOT

        # 6. Build python command (no auth — staging has xpack.security off)
        PYTHON_CMD+=" --es-host ${STAGING_URL} ${PYTHON_ARGS[*]}"
        local OUTPUT_SNAP="cluster_output_${TIMESTAMP}"

        echo "Step 6: Submitting clustering Slurm job ..."
        echo "  Command: ${PYTHON_CMD}"

        local SLURM_SCRIPT
        SLURM_SCRIPT=$(mktemp /tmp/cluster-XXXXXX.sbatch)

        cat > "$SLURM_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=whg-cluster
#SBATCH --output=${LOG_DIR}/cluster_%j.out
#SBATCH --error=${LOG_DIR}/cluster_%j.err
#SBATCH --time=${SLURM_TIME}
#SBATCH --partition=htc
#SBATCH --qos=normal
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=${SLURM_MEM}

set -e

echo "=========================================="
echo "PLACE CLUSTERING (Slurm → staging ES)"
echo "=========================================="
echo "Started: \$(date)"
echo "Node:    \$(hostname)"
echo "ES:      ${STAGING_URL} (staging)"
echo

# Verify staging ES is still alive and indices are ready
echo "Checking staging ES readiness ..."
if ! curl -sf --connect-timeout 10 "${STAGING_URL}/_cluster/health" >/dev/null 2>&1; then
    echo "ERROR: Staging ES at ${STAGING_URL} is not responding." >&2
    echo "  The staging Slurm job may have expired." >&2
    exit 1
fi
echo "  ES is responding. Waiting for indices to be ready ..."
# Poll until the places alias/index is queryable (up to 60 minutes).
# The login-node Step 5 already set replicas=0 and created aliases,
# but shards may still be initialising from the snapshot restore.
for ATTEMPT in \$(seq 1 360); do
    if curl -sf "${STAGING_URL}/places/_count" >/dev/null 2>&1; then
        echo "  ✓ Staging ES indices are ready"
        break
    fi
    if [ "\$ATTEMPT" -eq 360 ]; then
        echo "ERROR: places index not ready after 60 minutes." >&2
        echo "  Cluster health:" >&2
        curl -sf "${STAGING_URL}/_cluster/health?pretty" >&2 || true
        exit 1
    fi
    [ \$((\$ATTEMPT % 6)) -eq 0 ] && echo "    ... still waiting (\$((ATTEMPT/6)) min)"
    sleep 10
done

# Load environment
source "${CONDA_SETUP_PATH}"
conda activate whg
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"
cd "${REPO_DIR}"

${PYTHON_CMD}

echo
echo "Clustering finished. Snapshotting output indices ..."
curl -sf -X PUT "${STAGING_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${OUTPUT_SNAP}?wait_for_completion=true" \
    -H 'Content-Type: application/json' -d '{
  "indices": "clusters,cluster_state",
  "ignore_unavailable": true,
  "include_global_state": false
}'
echo
echo "=========================================="
echo "CLUSTERING COMPLETE"
echo "=========================================="
echo "Output snapshot: ${CLUSTER_EXCHANGE_REPO}/${OUTPUT_SNAP}"
echo "Finished: \$(date)"
echo
echo "Next step — push results to production:"
echo "  es -cluster-finalize ${TIMESTAMP}"
SBATCH_EOF

        JOBID=$(sbatch --parsable "$SLURM_SCRIPT")
        rm -f "$SLURM_SCRIPT"
        CLEAN_JOBID="${JOBID%;*}"

        echo
        echo "✓ Clustering job submitted: ${CLEAN_JOBID}"
        echo
        echo "Monitor with:"
        echo "  squeue -j ${CLEAN_JOBID}"
        echo "  tail -f ${LOG_DIR}/cluster_${CLEAN_JOBID}.out"
        echo
        echo "After the job completes, push results to production with:"
        echo "  es -cluster-finalize ${TIMESTAMP}"
        echo
        return 0
    fi

    # ---- nohup mode (on VM) ----
    local ES_URL="${PROD_ES_URL:-http://localhost:${PROD_ES_INTERNAL_PORT:-9201}}"
    local LOG_FILE="${LOG_DIR}/cluster_${TIMESTAMP}.log"

    [ -f "$PASS_FILE" ] && PYTHON_CMD+=" --es-pass-file ${PASS_FILE}"
    PYTHON_CMD+=" --es-host ${ES_URL} ${PYTHON_ARGS[*]}"

    # Verify ES is responding
    if ! es_curl --connect-timeout 5 "${ES_URL}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot connect to production ES at ${ES_URL}"
        return 1
    fi

    echo "=========================================="
    echo "PLACE CLUSTERING (nohup)"
    echo "=========================================="
    echo "ES:      ${ES_URL}"
    echo "Args:    ${PYTHON_ARGS[*]}"
    echo "Command: ${PYTHON_CMD}"
    echo "Log:     ${LOG_FILE}"
    echo

    # Write a small wrapper script so nohup runs in the right env
    local WRAPPER
    WRAPPER=$(mktemp /tmp/cluster-XXXXXX.sh)

    cat > "$WRAPPER" <<WRAPPER_EOF
#!/bin/bash
# Activate conda — try known paths, fall back to conda already in PATH
for _cs in "${CONDA_SETUP_PATH}" \\
           "\$HOME/miniconda3/etc/profile.d/conda.sh" \\
           "\$HOME/anaconda3/etc/profile.d/conda.sh"; do
    [ -f "\$_cs" ] && source "\$_cs" && break
done

conda activate whg 2>/dev/null || true
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"
cd "${REPO_DIR}"

${PYTHON_CMD}
WRAPPER_EOF

    chmod +x "$WRAPPER"

    # Launch under nohup; redirect stdout+stderr to log file
    nohup bash "$WRAPPER" > "$LOG_FILE" 2>&1 &
    local PID=$!

    # Clean up the temp script after a short delay (process has already read it)
    (sleep 5 && rm -f "$WRAPPER") &

    echo "Started PID ${PID}"
    echo "Safe to disconnect — the process will continue running."
    echo
    echo "Monitor with:"
    echo "  tail -f ${LOG_FILE}"
    echo
    echo "Check status:"
    echo "  ps -p ${PID} -o pid,etime,args"
    echo
    echo "--- Attaching to log (Ctrl-C to detach without stopping the job) ---"
    echo

    tail -f "$LOG_FILE" --pid="$PID"
}

do_cluster_finalize() {
    # Usage: es -cluster-finalize TIMESTAMP
    #
    # After a Slurm clustering job completes, this restores the output
    # snapshot (clusters, cluster_state) into production ES and performs
    # an atomic alias swap so production queries see the new data with
    # zero downtime.
    #
    # The TIMESTAMP is printed by  es -cluster --full --slurm  on submission.

    local TIMESTAMP="$1"
    if [ -z "$TIMESTAMP" ]; then
        echo "Usage: es -cluster-finalize TIMESTAMP"
        echo
        echo "TIMESTAMP was printed when you ran:  es -cluster --full --slurm"
        echo "It identifies the snapshot pair (cluster_input_TS / cluster_output_TS)."
        return 1
    fi

    # Detect production ES URL: localhost if on the VM, gateway otherwise
    local PROD_URL="${PROD_ES_URL:-http://localhost:${PROD_ES_INTERNAL_PORT:-9201}}"
    if ! es_curl --connect-timeout 3 "${PROD_URL}/_cluster/health" &>/dev/null; then
        PROD_URL="http://gazetteer.crcd.pitt.edu:${GATEWAY_PORT:-9200}"
    fi
    local SNAP_NAME="cluster_output_${TIMESTAMP}"

    echo "=========================================="
    echo "CLUSTER FINALIZE"
    echo "=========================================="
    echo "Production ES: ${PROD_URL}"
    echo "Snapshot:      ${CLUSTER_EXCHANGE_REPO}/${SNAP_NAME}"
    echo

    # 1. Verify production ES
    echo "Step 1: Checking production ES ..."
    if ! es_curl --connect-timeout 10 "${PROD_URL}/_cluster/health" &>/dev/null; then
        echo "ERROR: Cannot reach production ES at ${PROD_URL}"
        return 1
    fi
    echo "  ✓ Production ES reachable"
    echo

    # 2. Register exchange repo (idempotent) and verify snapshot exists
    echo "Step 2: Verifying output snapshot exists ..."
    # Force production ES to rediscover snapshots created by staging
    es_curl -s -X DELETE "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}" >/dev/null 2>&1 || true
    _ensure_exchange_repo "$PROD_URL" es_curl

    local SNAP_STATE
    SNAP_STATE=$(es_curl -sf "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/${SNAP_NAME}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['snapshots'][0]['state'])" 2>/dev/null || echo "MISSING")
    if [ "$SNAP_STATE" != "SUCCESS" ]; then
        echo "  ERROR: Snapshot ${SNAP_NAME} not found or not successful (state: ${SNAP_STATE})"
        echo "  Has the Slurm clustering job finished?"
        return 1
    fi
    echo "  ✓ Snapshot exists and is SUCCESS"
    echo

    # 3. Restore into dated indices (e.g. clusters_20260320, cluster_state_20260320)
    local DATE_SUFFIX
    DATE_SUFFIX=$(echo "$TIMESTAMP" | cut -c1-8)  # YYYYMMDD
    local NEW_CLUSTERS="clusters_${DATE_SUFFIX}"
    local NEW_STATE="cluster_state_${DATE_SUFFIX}"

    echo "Step 3: Restoring into dated indices ..."
    echo "  clusters      → ${NEW_CLUSTERS}"
    echo "  cluster_state → ${NEW_STATE}"

    # Delete targets if they already exist (e.g. re-running finalize)
    for idx in "$NEW_CLUSTERS" "$NEW_STATE"; do
        es_curl -sf -X DELETE "${PROD_URL}/${idx}" >/dev/null 2>&1 || true
    done

    local RENAME_BODY="\"rename_pattern\": \"(.+)\", \"rename_replacement\": \"\$1_${DATE_SUFFIX}\""
    _restore_snapshot "$PROD_URL" "$SNAP_NAME" "clusters,cluster_state" es_curl "$RENAME_BODY"
    _wait_for_restore "$PROD_URL" es_curl

    echo "  Index status:"
    es_curl -sf "${PROD_URL}/_cat/indices/${NEW_CLUSTERS},${NEW_STATE}?v&h=index,docs.count,store.size" 2>/dev/null || true
    echo

    # 4. Atomic alias swap
    echo "Step 4: Swapping aliases ..."

    # Build the alias actions:
    #   - Remove any existing alias pointing for 'clusters' / 'cluster_state'
    #   - Add alias for the new dated indices
    #   - Also handle the case where a concrete (non-aliased) index exists
    local ACTIONS='{"actions":['

    for pair in "clusters:${NEW_CLUSTERS}" "cluster_state:${NEW_STATE}"; do
        local ALIAS="${pair%%:*}"
        local TARGET="${pair##*:}"

        # Check if the alias currently exists
        local CURRENT_INDEX
        CURRENT_INDEX=$(es_curl -sf "${PROD_URL}/_alias/${ALIAS}" \
            | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).keys()))" 2>/dev/null || echo "")

        if [ -n "$CURRENT_INDEX" ] && [ "$CURRENT_INDEX" != "$TARGET" ]; then
            # Alias exists, pointing elsewhere → remove old, add new
            ACTIONS+='{"remove":{"index":"'"${CURRENT_INDEX}"'","alias":"'"${ALIAS}"'"}},'
            ACTIONS+='{"add":{"index":"'"${TARGET}"'","alias":"'"${ALIAS}"'"}},'
            echo "  ${ALIAS}: ${CURRENT_INDEX} → ${TARGET}"
        elif [ -z "$CURRENT_INDEX" ]; then
            # No alias. Check if a concrete index with that name exists.
            if es_curl -sf -o /dev/null "${PROD_URL}/${ALIAS}"; then
                echo "  ${ALIAS}: concrete index exists — deleting to make room for alias"
                es_curl -sf -X DELETE "${PROD_URL}/${ALIAS}" >/dev/null
            fi
            ACTIONS+='{"add":{"index":"'"${TARGET}"'","alias":"'"${ALIAS}"'"}},'
            echo "  ${ALIAS}: (new) → ${TARGET}"
        else
            echo "  ${ALIAS}: already points to ${TARGET} (no change)"
        fi
    done

    # Strip trailing comma, close
    ACTIONS="${ACTIONS%,}]}"

    if echo "$ACTIONS" | grep -q '"add"'; then
        es_curl -sf -X POST "${PROD_URL}/_aliases" \
            -H 'Content-Type: application/json' -d "$ACTIONS" >/dev/null
        echo "  ✓ Aliases swapped atomically"
    else
        echo "  (no alias changes needed)"
    fi
    echo

    # 5. Summary
    echo "=========================================="
    echo "FINALIZE COMPLETE"
    echo "=========================================="
    echo
    echo "Production aliases:"
    es_curl -sf "${PROD_URL}/_cat/aliases/clusters,cluster_state?v" 2>/dev/null || true
    echo
    echo "You may now clean up old dated indices if desired:"
    es_curl -sf "${PROD_URL}/_cat/indices/clusters_*,cluster_state_*?v&h=index,docs.count,store.size" 2>/dev/null || true
    echo
    echo "Cleaning up exchange snapshots ..."
    es_curl -sf -X DELETE "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/cluster_input_${TIMESTAMP}" >/dev/null 2>&1 \
        && echo "  ✓ Deleted cluster_input_${TIMESTAMP}" \
        || echo "  ⚠ cluster_input_${TIMESTAMP} not found (may already be cleaned)"
    es_curl -sf -X DELETE "${PROD_URL}/_snapshot/${CLUSTER_EXCHANGE_REPO}/cluster_output_${TIMESTAMP}" >/dev/null 2>&1 \
        && echo "  ✓ Deleted cluster_output_${TIMESTAMP}" \
        || echo "  ⚠ cluster_output_${TIMESTAMP} not found (may already be cleaned)"
}
