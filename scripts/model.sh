#!/bin/bash
# =============================================================================
# model.sh
# Phonetic Similarity Model Training Orchestrator for Pitt CRC
# =============================================================================
#
# Coordinates training of the phonetic similarity model on GPU nodes while
# reading training data from a staging Elasticsearch instance.
#
# v2 Updates:
#   - Namespace filtering (-n gn for GeoNames only)
#   - Cleanup invalid phonetics (--cleanup-phonetics)
#   - Curriculum hard negatives (Stage A/B for Phase 3)
#   - Uses modular phonetics package
#
# Prerequisites:
#   - Staging ES running: source es.sh -staging-start
#   - Conda environment with: torch, epitran, panphon, anyascii, elasticsearch
#
# Usage:
#   ./model.sh -extract -n gn        # Extract GeoNames only
#   ./model.sh -train                # Run all 3 training phases
#   ./model.sh -train-phase 3 -B     # Phase 3 with Stage B negatives
#   ./model.sh -status               # Check job status
#   ./model.sh -logs                 # View training logs
#
# =============================================================================

set -e

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IX1_BASE="/ix1/whcdh"
REPO_DIR="${IX1_BASE}/elastic"

# Load environment if available
ENV_FILE="${REPO_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# Staging ES info file (created by es.sh -staging-start)
STAGING_INFO_FILE="${IX1_BASE}/esinfo/es-staging.env"

# Model training directories
MODEL_DIR="${IX1_BASE}/models/phonetic"
DATA_DIR="${MODEL_DIR}/data"
CHECKPOINT_DIR="${MODEL_DIR}/checkpoints"
LOG_DIR="${MODEL_DIR}/logs"
SLURM_LOG_DIR="${MODEL_DIR}/slurm_logs"

# Job tracking
JOB_INFO_FILE="${MODEL_DIR}/current_job.sh"

# Training module location (now a package)
TRAINING_MODULE="phonetics"

# Default training parameters
DEFAULT_BATCH_SIZE=256
SUBSAMPLE_PAIRS=5000000  # Limit to 5M pairs
DEFAULT_MAX_DOCS=""  # Empty = all documents
DEFAULT_NAMESPACES=""  # Empty = all namespaces

# GPU partition settings
GPU_CLUSTER="gpu"
GPU_PARTITION="a100"
GPU_QOS="gpu-a100-l"
GPU_COUNT=1
GPU_MEM="40g"
CPU_COUNT=8
MEM="64G"
TIME_EXTRACT="48:00:00"
TIME_PHASE1="48:00:00"
TIME_PHASE2="48:00:00"
TIME_PHASE3="48:00:00"

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

ensure_directories() {
    mkdir -p "$DATA_DIR" "$CHECKPOINT_DIR" "$LOG_DIR" "$SLURM_LOG_DIR"
}

check_staging_es() {
    if [ ! -f "$STAGING_INFO_FILE" ]; then
        echo "ERROR: No staging ES instance found."
        echo "Start one first with: source es.sh -staging-start"
        return 1
    fi

    source "$STAGING_INFO_FILE"

    # Verify ES is responding
    if ! curl -s --connect-timeout 5 "http://${ES_NODE}:${ES_PORT}/_cluster/health" &>/dev/null; then
        echo "ERROR: Staging ES not responding at http://${ES_NODE}:${ES_PORT}"
        echo "Check status with: es.sh -staging-status"
        return 1
    fi

    echo "✓ Staging ES available at http://${ES_NODE}:${ES_PORT}"
    return 0
}

activate_conda() {
    cat <<'EOF'
# --- HARDCODED CONDA SETUP ---
CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"

if [ -f "$CONDA_SETUP" ]; then
    source "$CONDA_SETUP"
else
    echo "ERROR: Could not find conda setup at $CONDA_SETUP"
    export PATH="/ihome/whcdh/stg135/miniconda3/bin:$PATH"
fi

conda activate whg

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate 'whg' environment."
    exit 127
fi

echo "Environment: $(conda info --envs | grep '*' | awk '{print $1}')"
echo "Python: $(which python)"
EOF
}

# =============================================================================
# ENRICHMENT (hydrate toponyms with IPA/PanPhon) - UPDATED FOR PARALLEL
# =============================================================================

do_enrich() {
    echo "=========================================="
    echo "ENRICH ELASTICSEARCH TOPONYMS INDEX"
    echo "=========================================="
    echo

    check_staging_es || return 1
    ensure_directories

    source "$STAGING_INFO_FILE"

    # Parse arguments
    local NUM_WORKERS=12
    local BATCH_SIZE=5000

    while [[ $# -gt 0 ]]; do
        case $1 in
            --workers)
                NUM_WORKERS="$2"
                shift 2
                ;;
            --batch-size)
                BATCH_SIZE="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    local TIME_ENRICH="48:00:00"
    local ENRICH_SCRIPT=$(mktemp /tmp/phonetic-enrich-XXXXXX.sbatch)

    # Resources: 16 CPUs for 12 workers + overhead, 120G memory for ES bulk ops
    cat > "$ENRICH_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=phonetic-enrich
#SBATCH --partition=smp
#SBATCH --time=${TIME_ENRICH}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=120G
#SBATCH --output=${SLURM_LOG_DIR}/enrich-%j.out
#SBATCH --error=${SLURM_LOG_DIR}/enrich-%j.err

set -e

echo "=========================================="
echo "PHONETIC MODEL - ENRICHMENT (PARALLEL)"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo "CPUs: \$(nproc)"
echo "Workers: ${NUM_WORKERS}"
echo "Batch size: ${BATCH_SIZE}"
echo

source "$STAGING_INFO_FILE"

echo "Elasticsearch: http://\${ES_NODE}:\${ES_PORT}"
echo

$(activate_conda)

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

python -u -m ${TRAINING_MODULE} \\
    --enrich \\
    --es-host "\${ES_NODE}:\${ES_PORT}" \\
    --workers ${NUM_WORKERS} \\
    --batch-size ${BATCH_SIZE}

echo
echo "=========================================="
echo "ENRICHMENT COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
SBATCH_EOF

    echo "Submitting enrichment job..."
    echo "  Workers: ${NUM_WORKERS}"
    echo "  Batch size: ${BATCH_SIZE}"
    echo "  CPUs: 16"
    echo "  Memory: 120G"
    echo

    local JOBID=$(sbatch --parsable "$ENRICH_SCRIPT")
    rm "$ENRICH_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
CURRENT_PHASE="enrich"
STARTED_AT="$(date -Iseconds)"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${SLURM_LOG_DIR}/enrich-${JOBID}.out"
}

# =============================================================================
# DATA EXTRACTION
# =============================================================================

do_extract() {
    echo "=========================================="
    echo "EXTRACT TRAINING DATA FROM ELASTICSEARCH"
    echo "=========================================="
    echo

    check_staging_es || return 1
    ensure_directories

    source "$STAGING_INFO_FILE"

    # Parse arguments
    local MAX_DOCS_ARG=""
    local NAMESPACE_ARG=""
    local INDEX_NAME="places"
    local OUTPUT_SUFFIX=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --max-docs)
                MAX_DOCS_ARG="--max-docs $2"
                shift 2
                ;;
            -n|--namespaces)
                NAMESPACE_ARG="-n $2"
                OUTPUT_SUFFIX="_$2"
                shift 2
                ;;
            --index)
                INDEX_NAME="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    local OUTPUT_FILE="${DATA_DIR}/training_data${OUTPUT_SUFFIX}.h5"

    local EXTRACT_SCRIPT=$(mktemp /tmp/phonetic-extract-XXXXXX.sbatch)

    cat > "$EXTRACT_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=phonetic-extract
#SBATCH --partition=smp
#SBATCH --time=${TIME_EXTRACT}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --output=${SLURM_LOG_DIR}/extract-%j.out
#SBATCH --error=${SLURM_LOG_DIR}/extract-%j.err

set -e

echo "=========================================="
echo "PHONETIC MODEL - DATA EXTRACTION"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

source "$STAGING_INFO_FILE"

echo "Elasticsearch: http://\${ES_NODE}:\${ES_PORT}"
echo "Index: ${INDEX_NAME}"
echo "Namespaces: ${NAMESPACE_ARG:-ALL}"
echo "Output: ${OUTPUT_FILE}"
echo

$(activate_conda)

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

python -u -m ${TRAINING_MODULE} \\
    --phase 0 \\
    --es-host "\${ES_NODE}:\${ES_PORT}" \\
    --index "${INDEX_NAME}" \\
    --output "${OUTPUT_FILE}" \\
    ${NAMESPACE_ARG} \\
    ${MAX_DOCS_ARG}

echo
echo "=========================================="
echo "EXTRACTION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "Output: ${OUTPUT_FILE}"
ls -lh "${OUTPUT_FILE}"
SBATCH_EOF

    echo "Submitting extraction job..."
    local JOBID=$(sbatch --parsable "$EXTRACT_SCRIPT")
    rm "$EXTRACT_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
CURRENT_PHASE="extract"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${SLURM_LOG_DIR}/extract-${JOBID}.out"
}

# =============================================================================
# TRAINING (GPU jobs)
# =============================================================================

submit_training_phase() {
    local PHASE=$1
    local DEPENDENCY=$2
    local DATA_FILE=$3
    local CUSTOM_EPOCHS=$4
    local CUSTOM_BATCH_SIZE=$5
    local NEGATIVE_STAGE=$6
    local STAGE_A_MODEL=$7

    local PHASE_NAME=""
    local TIME_LIMIT=""
    local INPUT_ARGS=""
    local OUTPUT_FILE=""
    local STAGE_ARGS=""

    case $PHASE in
        1)
            PHASE_NAME="phase1-teacher"
            TIME_LIMIT="$TIME_PHASE1"
            INPUT_ARGS="--data ${DATA_FILE}"
            OUTPUT_FILE="${CHECKPOINT_DIR}/phase1.pt"
            ;;
        2)
            PHASE_NAME="phase2-alignment"
            TIME_LIMIT="$TIME_PHASE2"
            INPUT_ARGS="--data ${DATA_FILE} --phase1-model ${CHECKPOINT_DIR}/phase1.pt"
            OUTPUT_FILE="${CHECKPOINT_DIR}/phase2.pt"
            ;;
        3)
            if [ "$NEGATIVE_STAGE" = "B" ]; then
                PHASE_NAME="phase3-stage-b"
                OUTPUT_FILE="${CHECKPOINT_DIR}/final_model_b.pt"
                STAGE_ARGS="--negative-stage B --stage-a-model ${STAGE_A_MODEL:-${CHECKPOINT_DIR}/phase3_a.pt}"
            else
                PHASE_NAME="phase3-stage-a"
                OUTPUT_FILE="${CHECKPOINT_DIR}/phase3_a.pt"
                STAGE_ARGS="--negative-stage A"
            fi
            TIME_LIMIT="$TIME_PHASE3"
            INPUT_ARGS="--data ${DATA_FILE} --phase2-model ${CHECKPOINT_DIR}/phase2.pt"
            ;;
        *)
            echo "ERROR: Invalid phase: $PHASE"
            return 1
            ;;
    esac

    local DEPENDENCY_ARG=""
    if [ -n "$DEPENDENCY" ]; then
        DEPENDENCY_ARG="#SBATCH --dependency=afterok:${DEPENDENCY}"
    fi

    local EXTRA_ARGS=""
    if [ -n "$CUSTOM_EPOCHS" ]; then
        EXTRA_ARGS="$EXTRA_ARGS --epochs $CUSTOM_EPOCHS"
    fi
    if [ -n "$CUSTOM_BATCH_SIZE" ]; then
        EXTRA_ARGS="$EXTRA_ARGS --batch-size $CUSTOM_BATCH_SIZE"
    else
        EXTRA_ARGS="$EXTRA_ARGS --batch-size $DEFAULT_BATCH_SIZE"
    fi
    EXTRA_ARGS="$EXTRA_ARGS --subsample-pairs $SUBSAMPLE_PAIRS"

    local TRAIN_SCRIPT=$(mktemp /tmp/phonetic-train-p${PHASE}-XXXXXX.sbatch)

    cat > "$TRAIN_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=phonetic-${PHASE_NAME}
#SBATCH --cluster=${GPU_CLUSTER}
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --qos=${GPU_QOS}
#SBATCH --gres=gpu:${GPU_COUNT}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --time=${TIME_LIMIT}
#SBATCH --cpus-per-task=${CPU_COUNT}
#SBATCH --mem=${MEM}
#SBATCH --output=${SLURM_LOG_DIR}/${PHASE_NAME}-%j.out
#SBATCH --error=${SLURM_LOG_DIR}/${PHASE_NAME}-%j.err
${DEPENDENCY_ARG}

set -e

echo "=========================================="
echo "PHONETIC MODEL - PHASE ${PHASE} (${PHASE_NAME})"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

echo "--- GPU Information ---"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
echo

# === LOCAL SCRATCH SETUP ===
# Copy training data to node-local storage for faster I/O
if [ -n "\$SLURM_TMPDIR" ]; then
    echo "--- Setting up local scratch: \$SLURM_TMPDIR ---"
    LOCAL_DATA_DIR="\$SLURM_TMPDIR/phonetic_data"
    mkdir -p "\$LOCAL_DATA_DIR"

    # Copy optimized HDF5 files based on phase
    DATA_DIR="${DATA_DIR:-/ix1/whcdh/models/phonetic/data}"

    case ${PHASE} in
        1)
            echo "Copying Phase 1 data files..."
            cp -v "\$DATA_DIR"/training_data_*_optimized.h5 "\$LOCAL_DATA_DIR/" 2>/dev/null || true
            ;;
        2)
            echo "Copying Phase 2 data files..."
            cp -v "\$DATA_DIR"/*_optimized_phase2.h5 "\$LOCAL_DATA_DIR/" 2>/dev/null || true
            ;;
        3)
            echo "Copying Phase 3 data files..."
            cp -v "\$DATA_DIR"/*_optimized_phase3.h5 "\$LOCAL_DATA_DIR/" 2>/dev/null || true
            ;;
    esac

    echo "Local data files:"
    ls -lh "\$LOCAL_DATA_DIR"/*.h5 2>/dev/null || echo "No HDF5 files found"
    echo

    # Export for Python to detect
    export SLURM_TMPDIR
    export LOCAL_DATA_DIR
else
    echo "WARNING: SLURM_TMPDIR not set - using network storage (slower)"
fi
# === END LOCAL SCRATCH SETUP ===

$(activate_conda)

python -c "import torch; print(f'PyTorch: {torch.__version__}'); print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
echo

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Starting Training Phase ${PHASE} ---"
echo "Input: ${INPUT_ARGS}"
echo "Output: ${OUTPUT_FILE}"
echo "Stage args: ${STAGE_ARGS}"
echo

python -u -m ${TRAINING_MODULE} \\
    --phase ${PHASE} \\
    ${INPUT_ARGS} \\
    --output "${OUTPUT_FILE}" \\
    ${EXTRA_ARGS} \\
    ${STAGE_ARGS}

echo
echo "=========================================="
echo "PHASE ${PHASE} COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "Output: ${OUTPUT_FILE}"
ls -lh "${OUTPUT_FILE}"*
SBATCH_EOF

    local JOBID=$(sbatch --parsable "$TRAIN_SCRIPT" | cut -d';' -f1)
    rm "$TRAIN_SCRIPT"

    echo "$JOBID"
}

do_train() {
    echo "=========================================="
    echo "TRAIN PHONETIC SIMILARITY MODEL"
    echo "=========================================="
    echo

    ensure_directories

    # Parse arguments
    local SINGLE_PHASE=""
    local CUSTOM_DATA=""
    local CUSTOM_EPOCHS=""
    local CUSTOM_BATCH_SIZE=""
    local NEGATIVE_STAGE="A"
    local STAGE_A_MODEL=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --phase)
                SINGLE_PHASE="$2"
                shift 2
                ;;
            --data)
                CUSTOM_DATA="$2"
                shift 2
                ;;
            --epochs)
                CUSTOM_EPOCHS="$2"
                shift 2
                ;;
            --batch-size)
                CUSTOM_BATCH_SIZE="$2"
                shift 2
                ;;
            -A|--stage-a)
                NEGATIVE_STAGE="A"
                shift
                ;;
            -B|--stage-b)
                NEGATIVE_STAGE="B"
                shift
                ;;
            --stage-a-model)
                STAGE_A_MODEL="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    local DATA_FILE="${CUSTOM_DATA:-${DATA_DIR}/training_data.h5}"

#    if [ ! -f "${DATA_FILE}" ]; then
#        echo "ERROR: Training data not found at ${DATA_FILE}"
#        echo "Run extraction first: $0 -extract -n gn"
#        return 1
#    fi
#
#    echo "Training data: ${DATA_FILE}"
#    ls -lh "${DATA_FILE}"
#    echo

    if [ -n "$SINGLE_PHASE" ]; then
        echo "Training Phase ${SINGLE_PHASE} only..."

        if [ "$SINGLE_PHASE" = "3" ]; then
            echo "Negative stage: ${NEGATIVE_STAGE}"
        fi

        # Check prerequisites
        case $SINGLE_PHASE in
            2)
                if [ ! -f "${CHECKPOINT_DIR}/phase1.pt" ]; then
                    echo "ERROR: Phase 1 checkpoint not found. Run phase 1 first."
                    return 1
                fi
                ;;
            3)
                if [ ! -f "${CHECKPOINT_DIR}/phase2.pt" ]; then
                    echo "ERROR: Phase 2 checkpoint not found. Run phase 2 first."
                    return 1
                fi
                if [ "$NEGATIVE_STAGE" = "B" ] && [ ! -f "${STAGE_A_MODEL:-${CHECKPOINT_DIR}/phase3_a.pt}" ]; then
                    echo "ERROR: Stage A model not found for Stage B training."
                    echo "Either run Stage A first or specify --stage-a-model"
                    return 1
                fi
                ;;
        esac

        local JOBID=$(submit_training_phase "$SINGLE_PHASE" "" "$DATA_FILE" "$CUSTOM_EPOCHS" "$CUSTOM_BATCH_SIZE" "$NEGATIVE_STAGE" "$STAGE_A_MODEL")

        if [ -z "$JOBID" ]; then
            echo "ERROR: Failed to submit job"
            return 1
        fi

        cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
CURRENT_PHASE="phase${SINGLE_PHASE}"
NEGATIVE_STAGE="${NEGATIVE_STAGE}"
STARTED_AT="$(date -Iseconds)"
EOF

        echo "Submitted Phase ${SINGLE_PHASE} job: $JOBID"
        echo
        echo "Monitor with:"
        echo "  squeue -j $JOBID"
        echo "  tail -f ${SLURM_LOG_DIR}/*-${JOBID}.out"

    else
        # Train all phases with dependencies
        echo "Training all phases (1 → 2 → 3A) with job dependencies..."
        echo

        local JOB1=$(submit_training_phase 1 "" "$DATA_FILE" "$CUSTOM_EPOCHS" "$CUSTOM_BATCH_SIZE")
        echo "Phase 1 job: $JOB1"

        local JOB2=$(submit_training_phase 2 "$JOB1" "$DATA_FILE" "$CUSTOM_EPOCHS" "$CUSTOM_BATCH_SIZE")
        echo "Phase 2 job: $JOB2 (depends on $JOB1)"

        local JOB3=$(submit_training_phase 3 "$JOB2" "$DATA_FILE" "$CUSTOM_EPOCHS" "$CUSTOM_BATCH_SIZE" "A")
        echo "Phase 3A job: $JOB3 (depends on $JOB2)"

        cat > "$JOB_INFO_FILE" <<EOF
PHASE1_JOB_ID=$JOB1
PHASE2_JOB_ID=$JOB2
PHASE3_JOB_ID=$JOB3
CURRENT_PHASE="all"
STARTED_AT="$(date -Iseconds)"
EOF

        echo
        echo "=========================================="
        echo "TRAINING PIPELINE SUBMITTED"
        echo "=========================================="
        echo
        echo "Phase 1 (Teacher):      $JOB1"
        echo "Phase 2 (Alignment):    $JOB2 → depends on $JOB1"
        echo "Phase 3A (Curriculum):  $JOB3 → depends on $JOB2"
        echo
        echo "Optional: After Phase 3A completes, run Stage B:"
        echo "  $0 -train --phase 3 -B"
        echo
        echo "Monitor with:"
        echo "  $0 -status"
        echo "  $0 -logs"
    fi
}

# =============================================================================
# STATUS AND LOGS
# =============================================================================

do_status() {
    echo "=========================================="
    echo "PHONETIC MODEL TRAINING STATUS"
    echo "=========================================="
    echo

    if [ -f "$JOB_INFO_FILE" ]; then
        source "$JOB_INFO_FILE"
        echo "Current pipeline started: $STARTED_AT"
        echo "Phase: $CURRENT_PHASE"
        [ -n "$NEGATIVE_STAGE" ] && echo "Negative stage: $NEGATIVE_STAGE"
        echo

        if [ -n "$PHASE1_JOB_ID" ]; then
            echo "--- Job Queue Status ---"
            squeue -j "${PHASE1_JOB_ID},${PHASE2_JOB_ID},${PHASE3_JOB_ID}" 2>/dev/null || echo "Jobs not in queue (completed or failed)"
        elif [ -n "$CURRENT_JOB_ID" ]; then
            squeue -j "$CURRENT_JOB_ID" 2>/dev/null || echo "Job not in queue (completed or failed)"
        fi
    else
        echo "No active training pipeline."
    fi

    echo
    echo "--- Checkpoints ---"
    for f in phase1.pt phase2.pt phase3_a.pt final_model_b.pt; do
        if [ -f "${CHECKPOINT_DIR}/$f" ]; then
            echo "  ✓ $f  $(ls -lh ${CHECKPOINT_DIR}/$f | awk '{print $5, $6, $7, $8}')"
        else
            echo "  ✗ $f  (not found)"
        fi
    done

    echo
    echo "--- Training Data ---"
    for f in training_data.h5 training_data_gn.h5; do
        if [ -f "${DATA_DIR}/$f" ]; then
            echo "  ✓ $f  $(ls -lh ${DATA_DIR}/$f | awk '{print $5, $6, $7, $8}')"
        fi
    done
    [ ! -f "${DATA_DIR}/training_data.h5" ] && [ ! -f "${DATA_DIR}/training_data_gn.h5" ] && \
        echo "  ✗ No training data found - run extraction first"

    echo
    echo "--- Recent Logs ---"
    ls -lt "${SLURM_LOG_DIR}"/*.out 2>/dev/null | head -5 || echo "  No logs found"
}

do_logs() {
    local PHASE="${1:-latest}"

    echo "=========================================="
    echo "TRAINING LOGS"
    echo "=========================================="
    echo

    local LOG_FILE=""

    if [ "$PHASE" = "latest" ]; then
        LOG_FILE=$(ls -t "${SLURM_LOG_DIR}"/*.out 2>/dev/null | head -1)
    else
        LOG_FILE=$(ls -t "${SLURM_LOG_DIR}"/*phase${PHASE}*.out 2>/dev/null | head -1)
    fi

    if [ -z "$LOG_FILE" ] || [ ! -f "$LOG_FILE" ]; then
        echo "No log files found."
        echo "Available logs:"
        ls -lt "${SLURM_LOG_DIR}"/*.out 2>/dev/null | head -10 || echo "  (none)"
        return 1
    fi

    echo "Log file: $LOG_FILE"
    echo "=========================================="
    tail -100 "$LOG_FILE"
}

do_tail() {
    local LOG_FILE=$(ls -t "${SLURM_LOG_DIR}"/*.out 2>/dev/null | head -1)

    if [ -z "$LOG_FILE" ]; then
        echo "No log files found."
        return 1
    fi

    echo "Following: $LOG_FILE"
    echo "(Ctrl+C to stop)"
    echo "=========================================="
    tail -f "$LOG_FILE"
}

# =============================================================================
# INFERENCE TEST
# =============================================================================

do_test() {
    echo "=========================================="
    echo "TEST PHONETIC MODEL"
    echo "=========================================="
    echo

    local MODEL_FILE="${CHECKPOINT_DIR}/phase3_a.pt"

    if [ -f "${CHECKPOINT_DIR}/final_model_b.pt" ]; then
        MODEL_FILE="${CHECKPOINT_DIR}/final_model_b.pt"
    fi

    if [ ! -f "$MODEL_FILE" ]; then
        echo "ERROR: No trained model found."
        echo "Complete training first."
        return 1
    fi

    echo "Using model: $MODEL_FILE"
    echo

    srun --cluster=${GPU_CLUSTER} \
         --partition=${GPU_PARTITION} \
         --qos=${GPU_QOS} \
         --gres=gpu:${GPU_COUNT} \
         --mem=16G \
         --time=00:10:00 \
         --pty bash -c "
$(activate_conda)

cd $REPO_DIR
export PYTHONPATH=\"${REPO_DIR}:\${PYTHONPATH}\"

python -m ${TRAINING_MODULE} \\
    --infer \\
    --model ${MODEL_FILE} \\
    --toponym1 'London' --lang1 'en' \\
    --toponym2 'Londres' --lang2 'fr' \\
    --gpu

echo
echo 'Testing cross-script similarity...'
python -m ${TRAINING_MODULE} \\
    --infer \\
    --model ${MODEL_FILE} \\
    --toponym1 'Tokyo' --lang1 'en' \\
    --toponym2 '東京' --lang2 'ja' \\
    --gpu
"
}

# =============================================================================
# CLEANUP
# =============================================================================

do_clean() {
    echo "=========================================="
    echo "CLEANUP"
    echo "=========================================="
    echo

    echo "This will remove:"
    echo "  - Slurm log files: ${SLURM_LOG_DIR}/*.out, *.err"
    echo "  - Job info file: ${JOB_INFO_FILE}"
    echo
    echo "This will NOT remove:"
    echo "  - Training data: ${DATA_DIR}/"
    echo "  - Checkpoints: ${CHECKPOINT_DIR}/"
    echo

    read -p "Continue? (y/n): " confirm
    if [ "$confirm" != "y" ]; then
        echo "Cancelled."
        return 0
    fi

    rm -f "${SLURM_LOG_DIR}"/*.out "${SLURM_LOG_DIR}"/*.err
    rm -f "$JOB_INFO_FILE"

    echo "Cleanup complete."
}

do_clean_all() {
    echo "=========================================="
    echo "FULL CLEANUP (INCLUDING DATA)"
    echo "=========================================="
    echo

    echo "WARNING: This will remove EVERYTHING:"
    echo "  - Training data: ${DATA_DIR}/"
    echo "  - Checkpoints: ${CHECKPOINT_DIR}/"
    echo "  - Logs: ${LOG_DIR}/, ${SLURM_LOG_DIR}/"
    echo

    read -p "Are you sure? Type 'yes' to confirm: " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Cancelled."
        return 0
    fi

    rm -rf "$DATA_DIR" "$CHECKPOINT_DIR" "$LOG_DIR" "$SLURM_LOG_DIR" "$JOB_INFO_FILE"

    echo "Full cleanup complete."
}

# =============================================================================
# HELP
# =============================================================================

show_help() {
    cat <<EOF
Phonetic Similarity Model Training Orchestrator (v2)
=====================================================

Trains a phonetic embedding model for multilingual toponym matching using
GPU acceleration on Pitt CRC Slurm cluster.

v2 Features:
  - Namespace filtering (-n gn for GeoNames only)
  - Cleanup invalid cached phonetics
  - BiLSTM + Self-Attention architecture
  - Curriculum hard negatives (Stage A/B)

PREREQUISITES:
  1. Start staging ES: source es.sh -staging-start
  2. Ensure conda environment has: torch, epitran, panphon, anyascii, elasticsearch

USAGE: $0 COMMAND [OPTIONS]

DATA PREPARATION (requires staging ES):
  -enrich               Hydrate toponyms index with IPA/PanPhon features
  -cleanup-phonetics    Remove invalid cached phonetics (fixes earlier bugs)

  -extract [OPTIONS]    Extract training data from Elasticsearch
    -n, --namespaces NS   Namespace prefixes (e.g., -n gn or -n gn,wd)
    --max-docs N          Limit documents (for testing)
    --index NAME          Index name (default: places)

TRAINING (GPU jobs):
  -train                 Run all 3 phases (1 → 2 → 3A) with dependencies
  -train --phase N       Run specific phase (1, 2, or 3)

  Phase 3 curriculum options:
    -A, --stage-a        Stage A: ortho-close, phon-distant negatives (default)
    -B, --stage-b        Stage B: model-mined false positives
    --stage-a-model FILE Use specific Stage A model for Stage B mining

MONITORING:
  -status               Show training status and checkpoints
  -logs [PHASE]         Show recent log output
  -tail                 Follow latest log file

TESTING:
  -test                 Run quick inference test (interactive GPU)

CLEANUP:
  -clean                Remove logs and job info (keeps data/checkpoints)
  -clean-all            Remove everything including data and checkpoints

RECOMMENDED PIPELINE:
  # 1. Start staging ES
  source es.sh -staging-start

  # 2. Extract GeoNames only (faster, sufficient data)
  $0 -extract -n gn

  # 3. Train all phases
  $0 -train

  # 4. (Optional) Run Stage B for additional refinement
  $0 -train --phase 3 -B

  # 5. Test
  $0 -test

DIRECTORIES:
  Training data:   ${DATA_DIR}/
  Checkpoints:     ${CHECKPOINT_DIR}/
  Slurm logs:      ${SLURM_LOG_DIR}/

GPU RESOURCES:
  Cluster: ${GPU_CLUSTER}
  Partition: ${GPU_PARTITION}
  GPUs: ${GPU_COUNT}x A100 ${GPU_MEM}

EOF
}

# =============================================================================
# MAIN
# =============================================================================

case "$1" in
    -enrich)
        shift
        do_enrich "$@"
        ;;
    -extract)
        shift
        do_extract "$@"
        ;;
    -train)
        shift
        do_train "$@"
        ;;
    -train-phase)
        shift
        do_train --phase "$@"
        ;;
    -status)
        do_status
        ;;
    -logs)
        do_logs "$2"
        ;;
    -tail)
        do_tail
        ;;
    -test)
        do_test
        ;;
    -clean)
        do_clean
        ;;
    -clean-all)
        do_clean_all
        ;;
    -help|--help|help)
        show_help
        ;;
    *)
        show_help
        ;;
esac