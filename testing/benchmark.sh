#!/bin/bash
# =============================================================================
# benchmark.sh
# MEHDIE Benchmark Evaluation Orchestrator for Pitt CRC
# =============================================================================
#
# Evaluates phonetic similarity models against the MEHDIE benchmark testsets
# for cross-lingual historical toponym matching (Hebrew ↔ Arabic).
#
# Reference:
#   Sagi et al. (2025) "Utilizing phonetic similarity for cross-source and
#   cross-language toponym matching: a benchmark and prototype"
#   Language Resources and Evaluation, 59:2427-2451
#
# Usage:
#   ./benchmark.sh -baselines              # Run string similarity baselines (CPU)
#   ./benchmark.sh -model                  # Run trained model evaluation (GPU)
#   ./benchmark.sh -model --skip-baselines # Run model ONLY (skip string tests)
#   ./benchmark.sh -full                   # Run both baselines and model
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

# Model checkpoints
MODEL_DIR="${IX1_BASE}/models/phonetic/checkpoints"
DEFAULT_MODEL="${MODEL_DIR}/phase3_a.pt"

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
        echo
        echo "Please download the MEHDIE testsets and place them in:"
        echo "  $TESTSETS_DIR"
        echo
        echo "Expected structure:"
        echo "  $TESTSETS_DIR/"
        echo "    testset7-YaqutSham_KimaSham/"
        echo "      YaqutSham.tsv"
        echo "      KimaSham.tsv"
        echo "      em.tsv"
        echo "    testset8-KimaShamThurayyaSham/"
        echo "      ..."
        return 1
    fi

    # Check for at least one testset
    local found=0
    for dir in "$TESTSETS_DIR"/testset*; do
        if [ -d "$dir" ]; then
            found=1
            break
        fi
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
        echo
        echo "Train a model first with: ./model.sh -train"
        return 1
    fi

    # Check for vocabulary files
    local vocab_dir=$(dirname "$model_path")
    local base_name=$(basename "$model_path" .pt)

    if [ ! -f "${vocab_dir}/${base_name}_char_vocab.pkl" ]; then
        echo "ERROR: Character vocabulary not found"
        return 1
    fi

    if [ ! -f "${vocab_dir}/${base_name}_lang_vocab.pkl" ]; then
        echo "ERROR: Language vocabulary not found"
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

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate 'whg' environment."
    exit 127
fi

echo "Environment: $(conda info --envs | grep '*' | awk '{print $1}')"
echo "Python: $(which python)"
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

echo "=========================================="
echo "MEHDIE BENCHMARK - BASELINE EVALUATION"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo "CPUs: \$(nproc)"
echo

$(activate_conda)

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Baseline Evaluation ---"
echo "Testsets: ${TESTSETS_DIR}"
echo "Output: ${OUTPUT_FILE}"
echo

python -u -m ${TESTING_MODULE}.run_mehdie_evaluation \
    --testsets "${TESTSETS_DIR}" \
    --output "${OUTPUT_FILE}" \
    --device cpu

echo
echo "=========================================="
echo "BASELINE EVALUATION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "Results: ${OUTPUT_FILE}"
SBATCH_EOF

    echo "Submitting baseline evaluation job..."
    local JOBID=$(sbatch --parsable "$BASELINE_SCRIPT")
    rm "$BASELINE_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="baselines"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo "Output will be saved to: ${OUTPUT_FILE}"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID"
    echo "  tail -f ${LOG_DIR}/baselines-${JOBID}.out"
}

# =============================================================================
# MODEL EVALUATION (GPU)
# =============================================================================

do_model() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK - MODEL EVALUATION (GPU)"
    echo "=========================================="
    echo

    # Parse arguments
    local MODEL_PATH="$DEFAULT_MODEL"
    local SKIP_ARG=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --model)
                MODEL_PATH="$2"
                shift 2
                ;;
            --skip-baselines)
                SKIP_ARG="--skip-baselines"
                shift
                ;;
            *)
                shift
                ;;
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

echo "=========================================="
echo "MEHDIE BENCHMARK - MODEL EVALUATION"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

echo "--- GPU Information ---"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
echo

$(activate_conda)

python -c "import torch; print(f'PyTorch: {torch.__version__}'); print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
echo

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Model Evaluation ---"
echo "Testsets: ${TESTSETS_DIR}"
echo "Model: ${MODEL_PATH}"
echo "Output: ${OUTPUT_FILE}"
echo "Skipping baselines: ${SKIP_ARG:-No}"
echo

python -u -m ${TESTING_MODULE}.run_mehdie_evaluation \
    --testsets "${TESTSETS_DIR}" \
    --model "${MODEL_PATH}" \
    --output "${OUTPUT_FILE}" \
    --device cuda \
    ${SKIP_ARG}

echo
echo "=========================================="
echo "MODEL EVALUATION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "Results: ${OUTPUT_FILE}"
SBATCH_EOF

    echo "Submitting model evaluation job..."
    local JOBID=$(sbatch --parsable "$MODEL_SCRIPT" | cut -d';' -f1)
    rm "$MODEL_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="model"
MODEL_PATH="${MODEL_PATH}"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo "Output will be saved to: ${OUTPUT_FILE}"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID -c ${GPU_CLUSTER}"
    echo "  tail -f ${LOG_DIR}/model-${JOBID}.out"
}

# =============================================================================
# FULL EVALUATION (Baselines + Model)
# =============================================================================

do_full() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK - FULL EVALUATION"
    echo "=========================================="
    echo

    # Parse arguments
    local MODEL_PATH="$DEFAULT_MODEL"

    while [[ $# -gt 0 ]]; do
        case $1 in
            --model)
                MODEL_PATH="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    check_testsets || return 1
    check_model "$MODEL_PATH" || return 1
    ensure_directories

    local TIMESTAMP=$(get_timestamp)
    local OUTPUT_FILE="${RESULTS_DIR}/full_${TIMESTAMP}.json"

    # Full evaluation needs GPU for model inference
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

echo "=========================================="
echo "MEHDIE BENCHMARK - FULL EVALUATION"
echo "=========================================="
echo "Started: \$(date)"
echo "Node: \$(hostname)"
echo

echo "--- GPU Information ---"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
echo

$(activate_conda)

python -c "import torch; print(f'PyTorch: {torch.__version__}'); print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
echo

cd "$REPO_DIR"
export PYTHONPATH="${REPO_DIR}:\${PYTHONPATH}"

echo "--- Running Full Evaluation ---"
echo "Testsets: ${TESTSETS_DIR}"
echo "Model: ${MODEL_PATH}"
echo "Output: ${OUTPUT_FILE}"
echo
echo "This evaluates:"
echo "  - Levenshtein similarity"
echo "  - Jaro-Winkler similarity"
echo "  - Trained phonetic model"
echo

python -u -m ${TESTING_MODULE}.run_mehdie_evaluation \
    --testsets "${TESTSETS_DIR}" \
    --model "${MODEL_PATH}" \
    --output "${OUTPUT_FILE}" \
    --device cuda

echo
echo "=========================================="
echo "FULL EVALUATION COMPLETE"
echo "=========================================="
echo "Finished: \$(date)"
echo "Results: ${OUTPUT_FILE}"
SBATCH_EOF

    echo "Submitting full evaluation job..."
    local JOBID=$(sbatch --parsable "$FULL_SCRIPT" | cut -d';' -f1)
    rm "$FULL_SCRIPT"

    if [ -z "$JOBID" ]; then
        echo "ERROR: Failed to submit job"
        return 1
    fi

    cat > "$JOB_INFO_FILE" <<EOF
CURRENT_JOB_ID=$JOBID
JOB_TYPE="full"
MODEL_PATH="${MODEL_PATH}"
STARTED_AT="$(date -Iseconds)"
OUTPUT_FILE="${OUTPUT_FILE}"
EOF

    echo
    echo "Submitted job: $JOBID"
    echo "Output will be saved to: ${OUTPUT_FILE}"
    echo
    echo "Monitor with:"
    echo "  squeue -j $JOBID -c ${GPU_CLUSTER}"
    echo "  tail -f ${LOG_DIR}/full-${JOBID}.out"
}

# =============================================================================
# INTERACTIVE TEST (Quick validation)
# =============================================================================

do_test() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK - QUICK TEST"
    echo "=========================================="
    echo

    check_testsets || return 1

    echo "Running quick baseline test (interactive)..."
    echo

    # Run a quick test on SMP partition
    srun --partition=${SMP_PARTITION} \
         --time=00:15:00 \
         --cpus-per-task=2 \
         --mem=8G \
         --pty bash -c "
$(activate_conda)

cd $REPO_DIR
export PYTHONPATH=\"${REPO_DIR}:\${PYTHONPATH}\"

python -c \"
from testing.mehdie_benchmark import MEHDIEBenchmark, levenshtein_similarity

benchmark = MEHDIEBenchmark('${TESTSETS_DIR}')
print()
print('Running quick test on first testset...')
print()

if benchmark.testsets:
    testset_name = list(benchmark.testsets.keys())[0]
    results = benchmark.evaluate_model(
        levenshtein_similarity,
        thresholds=[0.8, 0.9],
        testset_names=[testset_name]
    )
    benchmark.print_results(results)
else:
    print('ERROR: No testsets loaded')
\"
"
}

# =============================================================================
# STATUS AND RESULTS
# =============================================================================

do_status() {
    echo "=========================================="
    echo "MEHDIE BENCHMARK STATUS"
    echo "=========================================="
    echo

    if [ -f "$JOB_INFO_FILE" ]; then
        source "$JOB_INFO_FILE"
        echo "Current job started: $STARTED_AT"
        echo "Job type: $JOB_TYPE"
        echo "Job ID: $CURRENT_JOB_ID"
        [ -n "$MODEL_PATH" ] && echo "Model: $MODEL_PATH"
        echo "Output: $OUTPUT_FILE"
        echo

        # Check job status
        if [ "$JOB_TYPE" = "baselines" ]; then
            squeue -j "$CURRENT_JOB_ID" 2>/dev/null || echo "Job not in queue (completed or failed)"
        else
            squeue -j "$CURRENT_JOB_ID" -c ${GPU_CLUSTER} 2>/dev/null || echo "Job not in queue (completed or failed)"
        fi
    else
        echo "No active evaluation job."
    fi

    echo
    echo "--- Testsets ---"
    if [ -d "$TESTSETS_DIR" ]; then
        for dir in "$TESTSETS_DIR"/testset*; do
            if [ -d "$dir" ]; then
                local name=$(basename "$dir")
                local em_file="$dir/em.tsv"
                if [ -f "$em_file" ]; then
                    local matches=$(grep -c "true" "$em_file" 2>/dev/null || echo "?")
                    echo "  ✓ $name ($matches matches)"
                else
                    echo "  ✗ $name (missing em.tsv)"
                fi
            fi
        done
    else
        echo "  ✗ Testsets directory not found"
    fi

    echo
    echo "--- Models ---"
    if [ -f "$DEFAULT_MODEL" ]; then
        echo "  ✓ Default model: $DEFAULT_MODEL"
    else
        echo "  ✗ Default model not found"
    fi

    # Check for alternative models
    for f in "${MODEL_DIR}"/phase3*.pt "${MODEL_DIR}"/final*.pt; do
        if [ -f "$f" ] && [ "$f" != "$DEFAULT_MODEL" ]; then
            echo "  ✓ Alternative: $f"
        fi
    done

    echo
    echo "--- Recent Results ---"
    ls -lt "${RESULTS_DIR}"/*.json 2>/dev/null | head -5 || echo "  No results found"
}

do_results() {
    local RESULT_FILE="${1:-latest}"

    echo "=========================================="
    echo "MEHDIE BENCHMARK RESULTS"
    echo "=========================================="
    echo

    if [ "$RESULT_FILE" = "latest" ]; then
        RESULT_FILE=$(ls -t "${RESULTS_DIR}"/*.json 2>/dev/null | head -1)
    fi

    if [ -z "$RESULT_FILE" ] || [ ! -f "$RESULT_FILE" ]; then
        echo "No result files found."
        echo "Available results:"
        ls -lt "${RESULTS_DIR}"/*.json 2>/dev/null | head -10 || echo "  (none)"
        return 1
    fi

    echo "Result file: $RESULT_FILE"
    echo "=========================================="

    # Pretty print JSON summary
    python3 -c "
import json
import sys

with open('$RESULT_FILE') as f:
    data = json.load(f)

print(f\"Timestamp: {data.get('timestamp', 'N/A')}\")
print(f\"Primary metric: {data.get('primary_metric', 'F-5')}\")
print()

# Print comparison summary
comparison = data.get('comparison', [])
if comparison:
    print('COMPARISON WITH MEHDIE PAPER:')
    print('-' * 80)
    print(f\"{'Testset':<35} {'MEHDIE':>10} {'Ours':>10} {'Δ':>8}\")
    print('-' * 80)

    for row in comparison:
        testset = row.get('testset', '').split('-')[0]
        mehdie = row.get('MEHDIE_best_f5', 'N/A')
        ours = row.get('OurModel_f5', row.get('Jaro-Winkler_f5', 'N/A'))

        if isinstance(mehdie, float) and isinstance(ours, float):
            delta = ours - mehdie
            print(f\"{testset:<35} {mehdie:>10.3f} {ours:>10.3f} {delta:>+8.3f}\")
        else:
            print(f\"{testset:<35} {str(mehdie):>10} {str(ours):>10} {'N/A':>8}\")
"
}

do_logs() {
    local LOG_FILE=$(ls -t "${LOG_DIR}"/*.out 2>/dev/null | head -1)

    if [ -z "$LOG_FILE" ]; then
        echo "No log files found."
        return 1
    fi

    echo "Log file: $LOG_FILE"
    echo "=========================================="
    tail -100 "$LOG_FILE"
}

do_tail() {
    local LOG_FILE=$(ls -t "${LOG_DIR}"/*.out 2>/dev/null | head -1)

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
# CLEANUP
# =============================================================================

do_clean() {
    echo "=========================================="
    echo "CLEANUP"
    echo "=========================================="
    echo

    echo "This will remove:"
    echo "  - Log files: ${LOG_DIR}/*.out, *.err"
    echo "  - Job info file: ${JOB_INFO_FILE}"
    echo
    echo "This will NOT remove:"
    echo "  - Results: ${RESULTS_DIR}/"
    echo "  - Testsets: ${TESTSETS_DIR}/"
    echo

    read -p "Continue? (y/n): " confirm
    if [ "$confirm" != "y" ]; then
        echo "Cancelled."
        return 0
    fi

    rm -f "${LOG_DIR}"/*.out "${LOG_DIR}"/*.err
    rm -f "$JOB_INFO_FILE"

    echo "Cleanup complete."
}

# =============================================================================
# HELP
# =============================================================================

show_help() {
    cat <<EOF
MEHDIE Benchmark Evaluation Orchestrator
=========================================

Evaluates phonetic similarity models against the MEHDIE benchmark testsets
for cross-lingual historical toponym matching (Hebrew ↔ Arabic).

Reference:
    Sagi et al. (2025) "Utilizing phonetic similarity for cross-source and
    cross-language toponym matching: a benchmark and prototype"
    Language Resources and Evaluation, 59:2427-2451

Primary Metric: F-5 (recall weighted 5x over precision)
    The paper notes users prefer high recall and tolerate low precision.

USAGE: $0 COMMAND [OPTIONS]

EVALUATION COMMANDS:
  -baselines            Run string similarity baselines only (CPU)
                        Methods: Levenshtein, Jaro-Winkler

  -model [--model PATH] Run trained model evaluation (GPU)
                        Default model: ${DEFAULT_MODEL}

  -model --skip-baselines
                        Run model evaluation ONLY (skipping string metrics)

  -full [--model PATH]  Run full evaluation (baselines + model)
                        Requires GPU for model inference

  -test                 Quick interactive test (validates setup)

MONITORING:
  -status               Show benchmark status and testset info
  -results [FILE]       View evaluation results (latest if no file specified)
  -logs                 Show recent log output
  -tail                 Follow latest log file

CLEANUP:
  -clean                Remove logs and job info (keeps results)

EXAMPLE WORKFLOW:
  # 1. Verify setup
  $0 -status

  # 2. Quick test
  $0 -test

  # 3. Run full evaluation
  $0 -full

  # 4. View results
  $0 -results

DIRECTORIES:
  Testsets:   ${TESTSETS_DIR}/
  Results:    ${RESULTS_DIR}/
  Logs:       ${LOG_DIR}/
  Models:     ${MODEL_DIR}/

TESTSETS (from paper Table 2):
  testset7:  YaqutSham ↔ KimaSham (30 matches)
  testset8:  KimaSham ↔ ThurayyaSham (21 matches)
  testset9:  Tudela ↔ Thurayya (18 matches)
  testset10: YaqutAndalusMagreb ↔ KimaMagrebAndalus (28 matches)
  testset11: Damast ↔ Tudela (32 matches)

PAPER RESULTS (Best F-5 at θ=0.9):
  Testset   Orthographic  Phonetic
  TS7       0.67          0.77
  TS8       0.65          0.68
  TS9       0.92          0.70
  TS10      0.75          0.77
  TS11      0.78          0.88

EOF
}

# =============================================================================
# MAIN
# =============================================================================

case "$1" in
    -baselines|--baselines)
        shift
        do_baselines "$@"
        ;;
    -model|--model)
        shift
        do_model "$@"
        ;;
    -full|--full)
        shift
        do_full "$@"
        ;;
    -test|--test)
        do_test
        ;;
    -status|--status)
        do_status
        ;;
    -results|--results)
        do_results "$2"
        ;;
    -logs|--logs)
        do_logs
        ;;
    -tail|--tail)
        do_tail
        ;;
    -clean|--clean)
        do_clean
        ;;
    -help|--help|help)
        show_help
        ;;
    *)
        show_help
        ;;
esac