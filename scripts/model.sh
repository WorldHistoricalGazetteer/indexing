#!/bin/bash
# =============================================================================
# model.sh
# Phonetic Similarity Model Training Orchestrator for Pitt CRC
# =============================================================================
#
# Coordinates training of the phonetic similarity model on GPU nodes while
# reading training data from a staging Elasticsearch instance.
#
# Prerequisites:
#   - Staging ES running: source es.sh -staging-start
#   - Conda environment with: torch, epitran, panphon, anyascii, elasticsearch
#
# Usage:
#   ./model.sh -extract          # Extract data from staging ES
#   ./model.sh -train            # Run all 3 training phases
#   ./model.sh -train-phase 1    # Run specific phase
#   ./model.sh -status           # Check job status
#   ./model.sh -logs             # View training logs
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

# Training script location
TRAINING_SCRIPT="${REPO_DIR}/phonetics/phonetic_similarity_model.py"

# Default training parameters
DEFAULT_BATCH_SIZE=256
SUBSAMPLE_PAIRS=5000000  # Limit to 5M pairs
DEFAULT_MAX_DOCS=""  # Empty = all documents

# GPU partition settings
GPU_PARTITION="a100"
GPU_COUNT=1
GPU_MEM="40g"
CPU_COUNT=8
MEM="64G"
TIME_EXTRACT="8:00:00"
TIME_PHASE1="18:00:00"  # ~15 hours (5M pairs, 50 epochs)
TIME_PHASE2="6:00:00"   # ~5 hours (no change, item-based)
TIME_PHASE3="12:00:00"  # ~10 hours (5M pairs, 20 epochs)

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

check_training_script() {
    if [ ! -f "$TRAINING_SCRIPT" ]; then
        echo "ERROR: Training script not found at $TRAINING_SCRIPT"
        echo "Please ensure phonetic_similarity_model.py is in the processing directory."
        return 1
    fi
    return 0
}

activate_conda() {
    cat <<'EOF'
# --- HARDCODED CONDA SETUP ---

# 1. Define the path to the SHELL SCRIPT (not the binary)
# Derived from: /ihome/whcdh/stg135/miniconda3/bin/conda
CONDA_SETUP="/ihome/whcdh/stg135/miniconda3/etc/profile.d/conda.sh"

# 2. Source it
if [ -f "$CONDA_SETUP" ]; then
    source "$CONDA_SETUP"
else
    echo "ERROR: Could not find conda setup at $CONDA_SETUP"
    # Fallback: try adding the bin directory to PATH directly
    export PATH="/ihome/whcdh/stg135/miniconda3/bin:$PATH"
fi

# 3. Activate
conda activate whg

# 4. Verify
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate 'whg' environment."
    exit 127
fi

echo "Environment: $(conda info --envs | grep '*' | awk '{print $1}')"
echo "Python: $(which python)"
EOF
}

# =============================================================================
# DATA EXTRACTION (CPU job - reads from ES)
# =============================================================================

do_enrich() {  # 27,393,380 toponyms in 4h26
    echo "=========================================="
    echo "ENRICH ELASTICSEARCH TOPONYMS INDEX"
    echo "=========================================="
    echo "This step hydrates the 'toponyms' index with computed IPA/Phonetic features."
    echo "It ensures subsequent extraction is purely I/O bound."
    echo

    check_staging_es || return 1
    check_training_script || return 1
    ensure_directories

    source "$STAGING_INFO_FILE"

    # Resource configuration for Enrichment
    # This is CPU-bound (Epitran) and Latency-bound (ES Updates)
    # 48 hours to handle the 74M document scan safely.
    local TIME_ENRICH="48:00:00"

    local ENRICH_SCRIPT=$(mktemp /tmp/phonetic-enrich-XXXXXX.sbatch)

    cat > "$ENRICH_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=phonetic-enrich
#SBATCH --partition=smp
#SBATCH --time=${TIME_ENRICH}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=${SLURM_LOG_DIR}/enrich-%j.out
#SBATCH --error=${SLURM_LOG_DIR}/enrich-%j.err

set -e

echo "=========================================="
echo "PHONETIC MODEL - ENRICHMENT (HYDRATION)"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

source "$STAGING_INFO_FILE"
export ES_HOST="http://\${ES_NODE}:\${ES_PORT}"

echo "Elasticsearch: \$ES_HOST"
echo "Task: Computing IPA and PanPhon features for supported languages"
echo

$(activate_conda)

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:${PYTHONPATH}"

echo "Starting enrichment loop..."
# Run the script in enrich mode
python -u "$TRAINING_SCRIPT" --enrich --es-host "\$ES_HOST"

echo
echo "=========================================="
echo "ENRICHMENT COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "The 'toponyms' index is now hydrated."
SBATCH_EOF

    echo "Submitting enrichment job..."
    local JOBID=$(sbatch --parsable "$ENRICH_SCRIPT")
    rm "$ENRICH_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    # Save job info
    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
CURRENT_PHASE="enrich"
STARTED_AT="$(date -Iseconds)"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo "This process is allocated 48 hours to handle the full dataset."
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${SLURM_LOG_DIR}/enrich-${JOBID}.out"
    echo
    echo "Once this completes, run '$0 -extract' to generate the training file."
}

do_extract() {
    echo "=========================================="
    echo "EXTRACT TRAINING DATA FROM ELASTICSEARCH"
    echo "=========================================="
    echo

    check_staging_es || return 1
    check_training_script || return 1
    ensure_directories

    source "$STAGING_INFO_FILE"

    # Parse arguments
    local MAX_DOCS_ARG=""
    local INDEX_NAME="places"

    while [[ $# -gt 0 ]]; do
        case $1 in
            --max-docs)
                MAX_DOCS_ARG="--max-docs $2"
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

    local OUTPUT_FILE="${DATA_DIR}/training_data.h5"

    # Create extraction job script
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

# Load staging ES info
source "$STAGING_INFO_FILE"
export ES_HOST="http://\${ES_NODE}:\${ES_PORT}"

echo "Elasticsearch: \$ES_HOST"
echo "Index: ${INDEX_NAME}"
echo "Output: ${OUTPUT_FILE}"
echo

$(activate_conda)

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:${PYTHONPATH}"

python -u "$TRAINING_SCRIPT" \\
    --phase 0 \\
    --es-host "http://\${ES_NODE}:\${ES_PORT}" \\
    --index "${INDEX_NAME}" \\
    --output "${OUTPUT_FILE}" \\
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

    # Save job info
    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
CURRENT_PHASE="extract"
STARTED_AT="$(date -Iseconds)"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${SLURM_LOG_DIR}/extract-${JOBID}.out"
    echo
    echo "When complete, run: $0 -train"
}

# =============================================================================
# TRAINING (GPU jobs)
# =============================================================================

submit_training_phase() {
    local PHASE=$1
    local DEPENDENCY=$2

    local PHASE_NAME=""
    local TIME_LIMIT=""
    local INPUT_ARGS=""
    local OUTPUT_FILE=""

    case $PHASE in
        1)
            PHASE_NAME="phase1-teacher"
            TIME_LIMIT="$TIME_PHASE1"
            INPUT_ARGS="--data ${DATA_DIR}/training_data.h5"
            OUTPUT_FILE="${CHECKPOINT_DIR}/phase1.pt"
            ;;
        2)
            PHASE_NAME="phase2-alignment"
            TIME_LIMIT="$TIME_PHASE2"
            INPUT_ARGS="--data ${DATA_DIR}/training_data.h5 --phase1-model ${CHECKPOINT_DIR}/phase1.pt"
            OUTPUT_FILE="${CHECKPOINT_DIR}/phase2.pt"
            ;;
        3)
            PHASE_NAME="phase3-generalize"
            TIME_LIMIT="$TIME_PHASE3"
            INPUT_ARGS="--data ${DATA_DIR}/training_data.h5 --phase2-model ${CHECKPOINT_DIR}/phase2.pt"
            OUTPUT_FILE="${CHECKPOINT_DIR}/final_model.pt"
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

    local TRAIN_SCRIPT=$(mktemp /tmp/phonetic-train-p${PHASE}-XXXXXX.sbatch)

    cat > "$TRAIN_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=phonetic-${PHASE_NAME}
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --time=${TIME_LIMIT}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${CPU_COUNT}
#SBATCH --mem=${MEM}
#SBATCH --gres=gpu:${GPU_COUNT}
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

# Show GPU info
echo "--- GPU Information ---"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
echo

$(activate_conda)

# Verify CUDA is available
python -c "import torch; print(f'PyTorch: {torch.__version__}'); print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
echo

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:${PYTHONPATH}"

echo "--- Starting Training Phase ${PHASE} ---"
echo "Input: ${INPUT_ARGS}"
echo "Output: ${OUTPUT_FILE}"
echo

python -u "$TRAINING_SCRIPT" \\
    --phase ${PHASE} \\
    ${INPUT_ARGS} \\
    --output "${OUTPUT_FILE}" \\
    --batch-size ${DEFAULT_BATCH_SIZE} \\
    --subsample-pairs ${SUBSAMPLE_PAIRS}

echo
echo "=========================================="
echo "PHASE ${PHASE} COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "Output: ${OUTPUT_FILE}"
ls -lh "${OUTPUT_FILE}"*
SBATCH_EOF

    local JOBID=$(sbatch --parsable "$TRAIN_SCRIPT")
    rm "$TRAIN_SCRIPT"

    echo "$JOBID"
}

do_train() {
    echo "=========================================="
    echo "TRAIN PHONETIC SIMILARITY MODEL"
    echo "=========================================="
    echo

    check_training_script || return 1
    ensure_directories

    # Parse arguments
    local SINGLE_PHASE=""
    local SKIP_EXTRACT=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --phase)
                SINGLE_PHASE="$2"
                shift 2
                ;;
            --skip-extract)
                SKIP_EXTRACT=true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    # Check training data exists
    if [ ! -f "${DATA_DIR}/training_data.h5" ]; then
        echo "ERROR: Training data not found at ${DATA_DIR}/training_data.h5"
        echo "Run extraction first: $0 -extract"
        return 1
    fi

    echo "Training data: ${DATA_DIR}/training_data.h5"
    ls -lh "${DATA_DIR}/training_data.h5"
    echo

    if [ -n "$SINGLE_PHASE" ]; then
        # Train single phase
        echo "Training Phase ${SINGLE_PHASE} only..."

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
                ;;
        esac

        local JOBID=$(submit_training_phase "$SINGLE_PHASE" "")

        if [ -z "$JOBID" ]; then
            echo "ERROR: Failed to submit job"
            return 1
        fi

        cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
CURRENT_PHASE="phase${SINGLE_PHASE}"
STARTED_AT="$(date -Iseconds)"
EOF

        echo "Submitted Phase ${SINGLE_PHASE} job: $JOBID"
        echo
        echo "Monitor with:"
        echo "  squeue -j $JOBID"
        echo "  tail -f ${SLURM_LOG_DIR}/*-${JOBID}.out"

    else
        # Train all phases with dependencies
        echo "Training all phases (1 → 2 → 3) with job dependencies..."
        echo

        local JOB1=$(submit_training_phase 1 "")
        echo "Phase 1 job: $JOB1"

        local JOB2=$(submit_training_phase 2 "$JOB1")
        echo "Phase 2 job: $JOB2 (depends on $JOB1)"

        local JOB3=$(submit_training_phase 3 "$JOB2")
        echo "Phase 3 job: $JOB3 (depends on $JOB2)"

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
        echo "Phase 3 (Generalize):   $JOB3 → depends on $JOB2"
        echo
        echo "Monitor with:"
        echo "  $0 -status"
        echo "  $0 -logs"
        echo
        echo "NOTE: Staging ES must remain running until extraction is complete."
        echo "      Training phases run on GPU nodes and don't need ES."
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

    # Show current jobs
    if [ -f "$JOB_INFO_FILE" ]; then
        source "$JOB_INFO_FILE"
        echo "Current pipeline started: $STARTED_AT"
        echo "Phase: $CURRENT_PHASE"
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
    for f in phase1.pt phase2.pt final_model.pt; do
        if [ -f "${CHECKPOINT_DIR}/$f" ]; then
            echo "  ✓ $f  $(ls -lh ${CHECKPOINT_DIR}/$f | awk '{print $5, $6, $7, $8}')"
        else
            echo "  ✗ $f  (not found)"
        fi
    done

    echo
    echo "--- Training Data ---"
    if [ -f "${DATA_DIR}/training_data.h5" ]; then
        echo "  ✓ training_data.h5  $(ls -lh ${DATA_DIR}/training_data.h5 | awk '{print $5, $6, $7, $8}')"
    else
        echo "  ✗ training_data.h5  (not found - run extraction first)"
    fi

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

    if [ ! -f "${CHECKPOINT_DIR}/final_model.pt" ]; then
        echo "ERROR: Final model not found at ${CHECKPOINT_DIR}/final_model.pt"
        echo "Complete training first."
        return 1
    fi

    # Quick interactive test
    echo "Running quick inference test..."
    echo

    # Use srun for interactive GPU access
    srun --partition=${GPU_PARTITION} \
         --gres=gpu:1 \
         --mem=16G \
         --time=00:10:00 \
         --pty bash -c "
$(activate_conda)

python $TRAINING_SCRIPT \\
    --infer \\
    --model ${CHECKPOINT_DIR}/final_model.pt \\
    --toponym1 'London' --lang1 'en' \\
    --toponym2 'Londres' --lang2 'fr' \\
    --gpu

echo
echo 'Testing cross-script similarity...'
python $TRAINING_SCRIPT \\
    --infer \\
    --model ${CHECKPOINT_DIR}/final_model.pt \\
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
Phonetic Similarity Model Training Orchestrator
================================================

Trains a phonetic embedding model for multilingual toponym matching using
GPU acceleration on Pitt CRC Slurm cluster.

PREREQUISITES:
  1. Start staging ES: source es.sh -staging-start
  2. Ensure conda environment has: torch, epitran, panphon, anyascii, elasticsearch

USAGE: $0 COMMAND [OPTIONS]

DATA EXTRACTION (requires staging ES):
  -extract [OPTIONS]     Extract training data from Elasticsearch
    --max-docs N         Limit to N documents (for testing)
    --index NAME         Index name (default: places)

TRAINING (GPU jobs):
  -train                 Run all 3 phases with job dependencies
  -train --phase N       Run specific phase (1, 2, or 3)

  Phase 1: Train phonetic encoder (Teacher) - ~12 hours
  Phase 2: Align character encoder to Teacher - ~8 hours
  Phase 3: Fine-tune on all data with hard negatives - ~6 hours

MONITORING:
  -status               Show training status and checkpoints
  -logs [PHASE]         Show recent log output
  -tail                 Follow latest log file

TESTING:
  -test                 Run quick inference test (interactive GPU)

CLEANUP:
  -clean                Remove logs and job info (keeps data/checkpoints)
  -clean-all            Remove everything including data and checkpoints

DIRECTORIES:
  Training data:   ${DATA_DIR}/
  Checkpoints:     ${CHECKPOINT_DIR}/
  Slurm logs:      ${SLURM_LOG_DIR}/

EXAMPLES:
  # Full pipeline
  source es.sh -staging-start          # Start ES (in another terminal)
  $0 -extract                          # Extract data (~1-2 hours)
  $0 -train                            # Train all phases (~26 hours total)
  $0 -status                           # Check progress
  $0 -test                             # Test the model

  # Quick test with limited data
  $0 -extract --max-docs 10000
  $0 -train

  # Resume from phase 2
  $0 -train --phase 2

GPU RESOURCES:
  Partition: ${GPU_PARTITION}
  GPUs: ${GPU_COUNT}x A100 ${GPU_MEM}
  CPUs: ${CPU_COUNT}
  Memory: ${MEM}

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
        # Alias for -train --phase
        do_train --phase "$2"
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