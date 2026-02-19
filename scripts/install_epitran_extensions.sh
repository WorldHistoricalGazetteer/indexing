#!/usr/bin/env bash
#
# Install Epitran extension CSV files to the active conda environment
#
# Usage: ./install_epitran_extensions.sh
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTENSIONS_DIR="$REPO_ROOT/phonetics/epitran_extensions"

# Detect Epitran installation directory
EPITRAN_DATA_DIR=$(python3 -c "import epitran, os; print(os.path.join(os.path.dirname(epitran.__file__), 'data', 'map'))")

if [[ ! -d "$EPITRAN_DATA_DIR" ]]; then
    echo "ERROR: Epitran data directory not found: $EPITRAN_DATA_DIR"
    echo "Is Epitran installed?"
    exit 1
fi

echo "=================================================="
echo "Installing Epitran Extensions"
echo "=================================================="
echo "Source: $EXTENSIONS_DIR"
echo "Target: $EPITRAN_DATA_DIR"
echo ""

# Count files
NUM_EXTENSIONS=$(find "$EXTENSIONS_DIR" -name "*.csv" | wc -l)
echo "Found $NUM_EXTENSIONS extension files to install"
echo ""

# Copy all .csv files
for csv_file in "$EXTENSIONS_DIR"/*.csv; do
    if [[ -f "$csv_file" ]]; then
        filename=$(basename "$csv_file")
        cp -v "$csv_file" "$EPITRAN_DATA_DIR/"
    fi
done

echo ""
echo "=================================================="
echo "✓ Installation complete"
echo "=================================================="
echo ""
echo "Verifying installation..."
python3 << 'PYEOF'
import epitran
import os

extensions_dir = os.path.join(os.path.dirname(epitran.__file__), 'data', 'map')
test_files = ['ell-Grek.csv', 'hye-Armn.csv', 'kan-Knda.csv', 'guj-Gujr.csv', 'jpn-Hrgn.csv', 'jpn-Ktkn.csv']

print("Checking key extension files:")
all_found = True
for filename in test_files:
    path = os.path.join(extensions_dir, filename)
    exists = os.path.exists(path)
    status = "✓" if exists else "✗ MISSING"
    print(f"  {status} {filename}")
    if not exists:
        all_found = False

if all_found:
    print("\n✓ All key extensions installed successfully")
else:
    print("\n✗ Some extensions are missing")
    exit(1)

# Test loading
print("\nTesting Epitran loading:")
test_cases = [
    ('ell-Grek', 'Αθήνα'),
    ('hye-Armn', 'Երևան'),
    ('kan-Knda', 'ಬೆಂಗಳೂರು'),
    ('guj-Gujr', 'અમદાવાદ'),
]

for code, sample in test_cases:
    try:
        epi = epitran.Epitran(code)
        result = epi.transliterate(sample)
        print(f"  ✓ {code}: {sample} → {result}")
    except Exception as e:
        print(f"  ✗ {code} FAILED: {e}")

PYEOF

echo ""
echo "Installation and verification complete!"

