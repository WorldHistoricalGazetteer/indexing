#!/bin/bash
# testing/batch-4b-validation.sh
#
# Batch 4b Canary Validation: staged extraction for nl + po without ES.
#
# Default behavior: submit itself to Slurm when launched from a login node.
# To force local execution (not recommended on CRC login nodes), pass --run-local.

set -eo pipefail

# CRC paths (override if needed)
REPO_ROOT="${WHG_REPO:-/ix1/ishi/elastic}"
CONDA_SH="${CONDA_SH:-/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh}"
CONDA_ENV="${CONDA_ENV:-whg}"
STAGED_BASE_DIR="${STAGED_BASE_DIR:-/vast/ishi/staged}"
LOG_DIR="${LOG_DIR:-/ix1/ishi/es/staging-logs}"

RUN_LOCAL=0
if [[ "${1:-}" == "--run-local" ]]; then
  RUN_LOCAL=1
fi

# Submit to Slurm by default when run from a login shell
if [[ -z "${SLURM_JOB_ID:-}" && "$RUN_LOCAL" -eq 0 ]]; then
  mkdir -p "$LOG_DIR"
  SUBMIT_CMD=(
    sbatch
    --job-name whg-b4b-validate
    --partition htc
    --qos htc-htc-s
    --time 04:00:00
    --nodes 1
    --ntasks 1
    --cpus-per-task 4
    --mem 16G
    --output "$LOG_DIR/whg-b4b-validate-%j.out"
    --error "$LOG_DIR/whg-b4b-validate-%j.err"
    --wrap "bash '$REPO_ROOT/testing/batch-4b-validation.sh' --run-local"
  )

  echo "Submitting Batch 4b validation to Slurm..."
  "${SUBMIT_CMD[@]}"
  echo "Submitted. Check logs in: $LOG_DIR"
  exit 0
fi

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

print_section() {
  echo -e "\n${YELLOW}========================================${NC}"
  echo -e "${YELLOW}$1${NC}"
  echo -e "${YELLOW}========================================${NC}\n"
}

print_ok() {
  echo -e "${GREEN}✓ $1${NC}"
}

print_fail() {
  echo -e "${RED}✗ $1${NC}"
}

unique_run_id() {
  local candidate
  while true; do
    candidate=$(python3 -c "from processing.staging_orchestrator import generate_run_id; print(generate_run_id())")
    if [[ ! -f "$STAGED_BASE_DIR/runs/${candidate}.json" ]]; then
      echo "$candidate"
      return 0
    fi
    sleep 1
  done
}

print_section "Activating Conda Environment"
source "$CONDA_SH"
conda activate "$CONDA_ENV"
cd "$REPO_ROOT"
print_ok "Environment activated: $CONDA_ENV"
print_ok "Repository: $(pwd)"
if [[ -n "${SLURM_JOB_ID:-}" ]]; then
  print_ok "Running under Slurm job: $SLURM_JOB_ID"
fi

print_section "Initializing Staged Run"
mkdir -p "$STAGED_BASE_DIR/runs"
export STAGED_BASE_DIR
export WHG_STAGING_MODE=1
RUN_ID=$(unique_run_id)
echo "Run ID: $RUN_ID"

# Single run for both canary namespaces (avoids manifest conflict)
print_section "Running staged extraction for nl + po"
python3 -m processing.ingest_all_authorities \
  --run-id "$RUN_ID" \
  --namespaces nl,po \
  --write-stage-snapshots \
  --materialize-namespace-manifest

# Check outputs
print_section "Verifying staged outputs"
NL_JSONL="$STAGED_BASE_DIR/nl/extract/places.jsonl"
PO_JSONL="$STAGED_BASE_DIR/po/extract/places.jsonl"

if [[ -f "$NL_JSONL" ]]; then
  NL_COUNT=$(wc -l < "$NL_JSONL")
  NL_SIZE=$(du -h "$NL_JSONL" | cut -f1)
  [[ "$NL_COUNT" -gt 0 ]] && print_ok "NativeLand output: $NL_COUNT lines, $NL_SIZE" || {
    print_fail "NativeLand extraction produced no documents"
    exit 1
  }
else
  print_fail "NativeLand JSONL not found: $NL_JSONL"
  exit 1
fi

if [[ -f "$PO_JSONL" ]]; then
  PO_COUNT=$(wc -l < "$PO_JSONL")
  PO_SIZE=$(du -h "$PO_JSONL" | cut -f1)
  [[ "$PO_COUNT" -gt 0 ]] && print_ok "Periodo output: $PO_COUNT lines, $PO_SIZE" || {
    print_fail "Periodo extraction produced no documents"
    exit 1
  }
else
  print_fail "Periodo JSONL not found: $PO_JSONL"
  exit 1
fi

print_section "Verifying run manifest"
MANIFEST="$STAGED_BASE_DIR/runs/$RUN_ID.json"
if [[ -f "$MANIFEST" ]]; then
  print_ok "Run manifest exists: $MANIFEST"
  python3 -c "import json; json.load(open('$MANIFEST'))"
  print_ok "Manifest JSON valid"
else
  print_fail "Run manifest not found: $MANIFEST"
  exit 1
fi

print_section "Test Summary"
echo -e "${GREEN}Batch 4b canary validation passed.${NC}"
echo "  nl: $NL_JSONL ($NL_SIZE, $NL_COUNT docs)"
echo "  po: $PO_JSONL ($PO_SIZE, $PO_COUNT docs)"
echo "  run manifest: $MANIFEST"
