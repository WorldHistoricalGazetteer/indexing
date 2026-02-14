#!/usr/bin/env bash
#
# Fast install of Epitran extension CSV files (no verification)
# Usage: ./fast_install_epitran_extensions.sh
#

set -euo pipefail

# Get Epitran data directory
EPITRAN_DATA_DIR=$(python3 -c "import epitran, os; print(os.path.join(os.path.dirname(epitran.__file__), 'data', 'map'))" 2>/dev/null)

if [[ ! -d "$EPITRAN_DATA_DIR" ]]; then
    echo "ERROR: Epitran data directory not found"
    exit 1
fi

EXTENSIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../phonetics/epitran_extensions" && pwd)"

if [[ ! -d "$EXTENSIONS_DIR" ]]; then
    echo "ERROR: Extensions directory not found: $EXTENSIONS_DIR"
    exit 1
fi

echo "Installing Epitran extensions..."
echo "Source: $EXTENSIONS_DIR"
echo "Target: $EPITRAN_DATA_DIR"

# Simple copy - fast, no frills
cp -v "$EXTENSIONS_DIR"/*.csv "$EPITRAN_DATA_DIR/" 2>&1 | head -10
NUM_FILES=$(ls -1 "$EXTENSIONS_DIR"/*.csv 2>/dev/null | wc -l)

echo ""
echo "✓ Installed $NUM_FILES extension files"
echo "Done!"

