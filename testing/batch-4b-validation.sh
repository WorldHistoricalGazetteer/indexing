#!/bin/bash
# testing/batch-4b-validation.sh
#
# Batch 4b Canary Validation: Test staged extraction for nl (NativeLand) and po (Periodo)
# without ES running.
#
# Usage:
#   bash testing/batch-4b-validation.sh
#
# Prerequisites:
#   - /ix1/ishi/elastic repo activated with conda whg environment
#   - Authority data files cached (nl/*.json, po/p0d.json)

set -eo pipefail

# CRC paths (override if needed)
REPO_ROOT="${WHG_REPO:-/ix1/ishi/elastic}"
CONDA_SH="${CONDA_SH:-/ihome/ishi/stg135/miniconda3/etc/profile.d/conda.sh}"
CONDA_ENV="${CONDA_ENV:-whg}"

# Staging config
STAGED_BASE_DIR="${STAGED_BASE_DIR:-/vast/ishi/staged}"
IX3_BASE="/vast/ishi"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Helper functions
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

# Activate environment
print_section "Activating Conda Environment"
source "$CONDA_SH"
conda activate "$CONDA_ENV"
cd "$REPO_ROOT"
print_ok "Environment activated: $CONDA_ENV"
print_ok "Repository: $(pwd)"

# Generate run ID
print_section "Initializing Staged Run"
RUN_ID=$(python3 -c "from processing.staging_orchestrator import generate_run_id; print(generate_run_id())")
echo "Run ID: $RUN_ID"

# Create staged directory structure
mkdir -p "$STAGED_BASE_DIR/runs"
export STAGED_BASE_DIR
export WHG_STAGING_MODE=1

# Test 1: nl (NativeLand)
print_section "Test 1: NativeLand (nl) Staged Extraction"
python3 -m processing.ingest_all_authorities \
  --run-id "$RUN_ID" \
  --namespaces nl \
  --write-stage-snapshots \
  --materialize-namespace-manifest

# Check nl output
NL_JSONL="$STAGED_BASE_DIR/nl/extract/places.jsonl"
if [[ -f "$NL_JSONL" ]]; then
  NL_COUNT=$(wc -l < "$NL_JSONL")
  NL_SIZE=$(du -h "$NL_JSONL" | cut -f1)
  print_ok "NativeLand output: $NL_COUNT lines, $NL_SIZE"
  if [[ $NL_COUNT -gt 0 ]]; then
    print_ok "NativeLand extraction successful"
  else
    print_fail "NativeLand extraction produced no documents"
    exit 1
  fi
else
  print_fail "NativeLand JSONL not found: $NL_JSONL"
  exit 1
fi

# Test 2: po (Periodo)
print_section "Test 2: Periodo (po) Staged Extraction"
python3 -m processing.ingest_all_authorities \
  --run-id "$RUN_ID" \
  --namespaces po \
  --write-stage-snapshots \
  --materialize-namespace-manifest

# Check po output
PO_JSONL="$STAGED_BASE_DIR/po/extract/places.jsonl"
if [[ -f "$PO_JSONL" ]]; then
  PO_COUNT=$(wc -l < "$PO_JSONL")
  PO_SIZE=$(du -h "$PO_JSONL" | cut -f1)
  print_ok "Periodo output: $PO_COUNT lines, $PO_SIZE"
  if [[ $PO_COUNT -gt 0 ]]; then
    print_ok "Periodo extraction successful"
  else
    print_fail "Periodo extraction produced no documents"
    exit 1
  fi
else
  print_fail "Periodo JSONL not found: $PO_JSONL"
  exit 1
fi

# Verify run manifest
print_section "Verifying Run Manifest"
MANIFEST="$STAGED_BASE_DIR/runs/$RUN_ID.json"
if [[ -f "$MANIFEST" ]]; then
  print_ok "Run manifest exists: $MANIFEST"
  # Quick JSON validation
  python3 -c "import json; json.load(open('$MANIFEST'))" && print_ok "Manifest JSON valid" || print_fail "Manifest JSON invalid"
else
  print_fail "Run manifest not found: $MANIFEST"
  exit 1
fi

# Summary
print_section "Test Summary"
echo -e "${GREEN}All Batch 4b canary tests passed!${NC}"
echo ""
echo "Staged extracts:"
echo "  nl: $NL_JSONL ($NL_SIZE, $NL_COUNT docs)"
echo "  po: $PO_JSONL ($PO_SIZE, $PO_COUNT docs)"
echo ""
echo "Run manifest: $MANIFEST"
echo ""
echo "Next steps:"
echo "  1. Review staged JSONL documents"
echo "  2. Submit Batch 6 H3 array job (see developer/batch-4-refactor-guide.md)"
echo "  3. Validate H3 patches merge correctly"

