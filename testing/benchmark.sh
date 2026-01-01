#!/bin/bash
# =============================================================================
# benchmark.sh
# MEHDIE Benchmark Evaluation Orchestrator for Pitt CRC
# =============================================================================
#
# Evaluates phonetic similarity models against:
# 1. External MEHDIE benchmark testsets (Hebrew ↔ Arabic)
# 2. Internal validation split (GeoNames)
#
# Reference:
#   Sagi et al. (2025) "Utilizing phonetic similarity for cross-source and
#   cross-language toponym matching: a benchmark and prototype"
#
# Usage:
#   ./benchmark.sh -baselines              # Run string similarity baselines (CPU)
#   ./benchmark.sh -model                  # Run trained model on MEHDIE (GPU)
#   ./benchmark.sh -val                    # Run model on Internal Validation Split (GPU)
#   ./benchmark.sh -full                   # Run MEHDIE baselines + model
#   ./benchmark.sh -status                 # Check job status
#   ./benchmark.sh -results                # View latest results
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

# Benchmark directories
BENCHMARK_DIR="${REPO_DIR}/testing"
TESTSETS_DIR="${BENCHMARK_DIR}/mehdie-testsets"
RESULTS_DIR="${BENCHMARK_DIR}/results"
LOG_DIR="${BENCHMARK_DIR}/logs"

# Data & Models
MODEL_DIR="${IX1_BASE}/models/phonetic/checkpoints"
DATA_DIR="${IX1_BASE}/models/phonetic/data"
DEFAULT_MODEL="${MODEL_DIR}/final_model_b.pt"
DEFAULT_TRAIN_DATA="${DATA_DIR}/training_data_gn_optimized_phase3.h5"

# Job tracking
JOB_INFO_FILE="${BENCHMARK_DIR}/current_job.sh"

# Testing module location
TESTING_MODULE="testing"

# Slurm settings
SMP_PARTITION="smp"
SMP_TIME="04:00:00"
SMP_MEM="32G"
SMP_CPUS=4

GPU_CLUSTER="gpu"
GPU_PARTITION="a100"
GPU_QOS="gpu-a100-s"  # Short queue for evaluation
GPU_COUNT=1
GPU_TIME="02:00:00"
GPU_MEM="32G"
GPU_CPUS=4

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

ensure_directories() {
    mkdir -p "$RESULTS_DIR" "$LOG_DIR"
}

check_testsets() {
    if [ ! -d "$TESTSETS_DIR" ]; then
        echo "ERROR: MEHDIE testsets not found at: $TESTSETS_DIR"
        return 1
    fi
    # Check for at least one testset
    local found=0
    for dir in "$TESTSETS_DIR"/testset*; do
        if [ -d "$dir" ]; then found=1; break; fi
    done
    if [ $found -eq 0 ]; then
        echo "ERROR: No testset directories found in $TESTSETS_DIR"
        return 1
    fi
    echo "✓ MEHDIE testsets found at: $TESTSETS_DIR"
    return 0
}

check_model() {
    local model_path="${1:-$DEFAULT_MODEL}"
    if [ ! -f "$model_path" ]; then
        echo "ERROR: Model checkpoint not found at: $model_path"
        return 1
    fi
    # Check for vocabulary files
    local vocab_dir=$(dirname "$model_path")
    local base_name=$(basename "$model_path" .pt)
    if [ ! -f "${vocab_dir}/${base_name}_char_vocab.pkl" ]; then
        echo "ERROR: Character vocabulary not found"
        return 1
    fi
    echo "✓ Model and vocabularies found at: $model_path"
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
EOF
}

get_timestamp() {
    date +"%Y%m%d_%H%M%S"
}

# =============================================================================
# BASELINE EVALUATION (CPU)
# =============================================================================

do_baselines() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK - BASELINE METHODS (CPU)"
    echo "=========================================="
    echo

    check_testsets || return 1
    ensure_directories

    local TIMESTAMP=$(get_timestamp)
    local OUTPUT_FILE="${RESULTS_DIR}/baselines_${TIMESTAMP}.json"
    local BASELINE_SCRIPT=$(mktemp /tmp/mehdie-baseline-XXXXXX.sbatch)

    cat > "$BASELINE_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=mehdie-baselines
#SBATCH --partition=${SMP_PARTITION}
#SBATCH --time=${SMP_TIME}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${SMP_CPUS}
#SBATCH --mem=${SMP_MEM}
#SBATCH --output=${LOG_DIR}/baselines-%j.out
#SBATCH --error=${LOG_DIR}/baselines-%j.err

set -e
$(activate_conda)
cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Baseline Evaluation ---"
python -u -m ${TESTING_MODULE}.run_mehdie_evaluation \
    --testsets "${TESTSETS_DIR}" \
    --output "${OUTPUT_FILE}" \
    --device cpu

echo "Results: ${OUTPUT_FILE}"
SBATCH_EOF

    local JOBID=$(sbatch --parsable "$BASELINE_SCRIPT")
    rm "$BASELINE_SCRIPT"
    [ -z "$JOBID" ] && return 1

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="baselines"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF
    echo "Submitted job: $JOBID"
}

# =============================================================================
# INTERNAL VALIDATION SPLIT (GPU)
# =============================================================================

do_val_baselines() {
    echo "=========================================="
    echo "VALIDATION SPLIT - BASELINES (CPU)"
    echo "=========================================="
    echo

    local DATA_PATH="$DEFAULT_TRAIN_DATA"
    local MAX_PAIRS=50000

    while [[ $# -gt 0 ]]; do
        case $1 in
            --data) DATA_PATH="$2"; shift 2 ;;
            --max-pairs) MAX_PAIRS="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    if [ ! -f "$DATA_PATH" ]; then
        echo "ERROR: Training data not found at: $DATA_PATH"
        return 1
    fi
    ensure_directories

    local BASELINE_SCRIPT=$(mktemp /tmp/val-baselines-XXXXXX.sbatch)

    cat > "$BASELINE_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=val-baselines
#SBATCH --partition=${SMP_PARTITION}
#SBATCH --time=${SMP_TIME}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${SMP_CPUS}
#SBATCH --mem=${SMP_MEM}
#SBATCH --output=${LOG_DIR}/val_baselines-%j.out
#SBATCH --error=${LOG_DIR}/val_baselines-%j.err

set -e
$(activate_conda)
cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Baseline Evaluation on Val Split ---"
echo "Data: ${DATA_PATH}"
echo "Max pairs: ${MAX_PAIRS}"
echo

python -u -m ${TESTING_MODULE}.evaluate_val_split_baselines \
    --data "${DATA_PATH}" \
    --max-pairs ${MAX_PAIRS}

echo
echo "Done."
SBATCH_EOF

    echo "Submitting baseline validation job..."
    local JOBID=$(sbatch --parsable "$BASELINE_SCRIPT")
    rm "$BASELINE_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="val_baselines"
DATA_PATH="${DATA_PATH}"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${LOG_DIR}/val_baselines-${JOBID}.out"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo "Results will be in: ${LOG_DIR}/val_baselines-${JOBID}.out"
}

do_val_split() {
    echo "=========================================="
    echo "INTERNAL VALIDATION EVALUATION (GPU)"
    echo "=========================================="
    echo

    # Parse arguments
    local MODEL_PATH="$DEFAULT_MODEL"
    local DATA_PATH="$DEFAULT_TRAIN_DATA"

    while [[ $# -gt 0 ]]; do
        case $1 in
            --model) MODEL_PATH="$2"; shift 2 ;;
            --data)  DATA_PATH="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    check_model "$MODEL_PATH" || return 1
    if [ ! -f "$DATA_PATH" ]; then
        echo "ERROR: Training data not found at: $DATA_PATH"
        return 1
    fi
    ensure_directories

    local VAL_SCRIPT=$(mktemp /tmp/val-split-XXXXXX.sbatch)

    cat > "$VAL_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=val-split
#SBATCH --cluster=${GPU_CLUSTER}
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --qos=${GPU_QOS}
#SBATCH --gres=gpu:${GPU_COUNT}
#SBATCH --time=${GPU_TIME}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${GPU_CPUS}
#SBATCH --mem=${GPU_MEM}
#SBATCH --output=${LOG_DIR}/val_split-%j.out
#SBATCH --error=${LOG_DIR}/val_split-%j.err

set -e
echo "--- GPU Information ---"
nvidia-smi --query-gpu=name,memory.total --format=csv

$(activate_conda)
cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Validation Split Evaluation ---"
echo "Model: ${MODEL_PATH}"
echo "Data:  ${DATA_PATH}"
echo

python -u -m ${TESTING_MODULE}.evaluate_val_split \
    --model "${MODEL_PATH}" \
    --data "${DATA_PATH}" \
    --gpu \
    --max-negatives 500000

echo
echo "Done."
SBATCH_EOF

    echo "Submitting validation evaluation job..."
    local JOBID=$(sbatch --parsable "$VAL_SCRIPT" | cut -d';' -f1)
    rm "$VAL_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="val_split"
MODEL_PATH="${MODEL_PATH}"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${LOG_DIR}/val_split-${JOBID}.out"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo "Results will be in: ${LOG_DIR}/val_split-${JOBID}.out"
    echo "Monitor with: squeue -j $JOBID -c ${GPU_CLUSTER}"
}

# =============================================================================
# MODEL EVALUATION (GPU)
# =============================================================================

do_model() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK - MODEL EVALUATION (GPU)"
    echo "=========================================="
    echo

    local MODEL_PATH="$DEFAULT_MODEL"
    local SKIP_ARG=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --model) MODEL_PATH="$2"; shift 2 ;;
            --skip-baselines) SKIP_ARG="--skip-baselines"; shift ;;
            *) shift ;;
        esac
    done

    check_testsets || return 1
    check_model "$MODEL_PATH" || return 1
    ensure_directories

    local TIMESTAMP=$(get_timestamp)
    local OUTPUT_FILE="${RESULTS_DIR}/model_${TIMESTAMP}.json"
    local MODEL_SCRIPT=$(mktemp /tmp/mehdie-model-XXXXXX.sbatch)

    cat > "$MODEL_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=mehdie-model
#SBATCH --cluster=${GPU_CLUSTER}
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --qos=${GPU_QOS}
#SBATCH --gres=gpu:${GPU_COUNT}
#SBATCH --time=${GPU_TIME}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${GPU_CPUS}
#SBATCH --mem=${GPU_MEM}
#SBATCH --output=${LOG_DIR}/model-%j.out
#SBATCH --error=${LOG_DIR}/model-%j.err

set -e
echo "--- GPU Information ---"
nvidia-smi --query-gpu=name,memory.total --format=csv

$(activate_conda)
cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Model Evaluation ---"
python -u -m ${TESTING_MODULE}.run_mehdie_evaluation \
    --testsets "${TESTSETS_DIR}" \
    --model "${MODEL_PATH}" \
    --output "${OUTPUT_FILE}" \
    --device cuda \
    ${SKIP_ARG}

echo "Results: ${OUTPUT_FILE}"
SBATCH_EOF

    local JOBID=$(sbatch --parsable "$MODEL_SCRIPT" | cut -d';' -f1)
    rm "$MODEL_SCRIPT"
    [ -z "$JOBID" ] && return 1

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="model"
MODEL_PATH="${MODEL_PATH}"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF
    echo "Submitted job: $JOBID"
}

# =============================================================================
# FULL EVALUATION
# =============================================================================

do_full() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK - FULL EVALUATION"
    echo "=========================================="
    echo
    local MODEL_PATH="$DEFAULT_MODEL"
    while [[ $# -gt 0 ]]; do
        case $1 in
            --model) MODEL_PATH="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    check_testsets || return 1
    check_model "$MODEL_PATH" || return 1
    ensure_directories

    local TIMESTAMP=$(get_timestamp)
    local OUTPUT_FILE="${RESULTS_DIR}/full_${TIMESTAMP}.json"
    local FULL_SCRIPT=$(mktemp /tmp/mehdie-full-XXXXXX.sbatch)

    cat > "$FULL_SCRIPT" <<SBATCH_EOF
#!/bin/bash
#SBATCH --job-name=mehdie-full
#SBATCH --cluster=${GPU_CLUSTER}
#SBATCH --partition=${GPU_PARTITION}
#SBATCH --qos=${GPU_QOS}
#SBATCH --gres=gpu:${GPU_COUNT}
#SBATCH --time=${GPU_TIME}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${GPU_CPUS}
#SBATCH --mem=${GPU_MEM}
#SBATCH --output=${LOG_DIR}/full-%j.out
#SBATCH --error=${LOG_DIR}/full-%j.err

set -e
$(activate_conda)
cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Full Evaluation ---"
python -u -m ${TESTING_MODULE}.run_mehdie_evaluation \
    --testsets "${TESTSETS_DIR}" \
    --model "${MODEL_PATH}" \
    --output "${OUTPUT_FILE}" \
    --device cuda

echo "Results: ${OUTPUT_FILE}"
SBATCH_EOF

    local JOBID=$(sbatch --parsable "$FULL_SCRIPT" | cut -d';' -f1)
    rm "$FULL_SCRIPT"
    [ -z "$JOBID" ] && return 1

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="full"
MODEL_PATH="${MODEL_PATH}"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF
    echo "Submitted job: $JOBID"
}

# =============================================================================
# STATUS & UTILS
# =============================================================================

do_status() {
    echo "=========================================="
    echo "BENCHMARK STATUS"
    echo "=========================================="
    if [ -f "$JOB_INFO_FILE" ]; then
        source "$JOB_INFO_FILE"
        echo "Job ID: $CURRENT_JOB_ID ($JOB_TYPE)"
        echo "Started: $STARTED_AT"
        echo "Output: $OUTPUT_FILE"
        if [ "$JOB_TYPE" = "baselines" ]; then
            squeue -j "$CURRENT_JOB_ID" 2>/dev/null || echo "Job completed/failed"
        else
            squeue -j "$CURRENT_JOB_ID" -c ${GPU_CLUSTER} 2>/dev/null || echo "Job completed/failed"
        fi
    else
        echo "No active job."
    fi
    echo
    echo "Default Model: $DEFAULT_MODEL"
    echo "Train Data:    $DEFAULT_TRAIN_DATA"
}

do_results() {
    local RESULT_FILE="${1:-latest}"
    if [ "$RESULT_FILE" = "latest" ]; then
        RESULT_FILE=$(ls -t "${RESULTS_DIR}"/*.json 2>/dev/null | head -1)
    fi
    if [ -z "$RESULT_FILE" ]; then echo "No results found."; return 1; fi

    echo "Result: $RESULT_FILE"
    python3 -c "
import json
with open('$RESULT_FILE') as f:
    d = json.load(f)
print(f\"Metric: {d.get('primary_metric', 'F5')}\")
for r in d.get('comparison', []):
    print(f\"{r.get('testset','?'):<30} MEHDIE:{r.get('MEHDIE_best_f5','?'):>6} Ours:{r.get('OurModel_f5','?'):>6}\")
"
}

do_logs() {
    ls -t "${LOG_DIR}"/*.out 2>/dev/null | head -1 | xargs tail -100
}

do_tail() {
    ls -t "${LOG_DIR}"/*.out 2>/dev/null | head -1 | xargs tail -f
}

do_test() {
    check_testsets || return 1
    srun --partition=${SMP_PARTITION} --time=00:15:00 --pty bash -c "$(activate_conda); cd $REPO_DIR; export PYTHONPATH=${REPO_DIR}:\${PYTHONPATH}; python -c 'from testing.mehdie_benchmark import MEHDIEBenchmark; print(\"Imports OK\")'"
}

show_help() {
    cat <<EOF
USAGE: $0 COMMAND [OPTIONS]

COMMANDS:
  -baselines            Run string similarity baselines (CPU)
  -model [--model P]    Run trained model on MEHDIE benchmark (GPU)
  -val [--data P]       Run model on Internal Validation Split (GPU)
  -full                 Run full benchmark (Baselines + Model)
  -status               Check status
  -results              View results
  -logs | -tail         View logs

DEFAULTS:
  Model: ${DEFAULT_MODEL}
  Data:  ${DEFAULT_TRAIN_DATA}
EOF
}

# =============================================================================
# MAIN
# =============================================================================

case "$1" in
    -baselines|--baselines) shift; do_baselines "$@" ;;
    -model|--model)         shift; do_model "$@" ;;
    -val|--val)             shift; do_val_split "$@" ;;
    -val-baselines|--val-baselines) shift; do_val_baselines "$@" ;;
    -full|--full)           shift; do_full "$@" ;;
    -status|--status)       do_status ;;
    -results|--results)     do_results "$2" ;;
    -logs|--logs)           do_logs ;;
    -tail|--tail)           do_tail ;;
    -test|--test)           do_test ;;
    -help|--help|*)         show_help ;;
esac