#!/bin/bash

# Usage: bash build-ijde.sh [--deanon]
# Default: builds symphonym_ijde_anonymised (for peer review submission)
# --deanon: builds symphonym_ijde (camera-ready / post-acceptance)

if [[ "$1" == "--deanon" ]]; then
    BASE="symphonym_ijde"
    echo "Building DE-ANONYMISED version: ${BASE}.tex"
else
    BASE="symphonym_ijde_anonymised"
    echo "Building ANONYMISED version: ${BASE}.tex"
fi

echo "1. Cleaning previous build artifacts..."
rm -f pdf/${BASE}.pdf

echo "2. Compiling with latexmk..."
latexmk -f ${BASE}.tex

echo "3. Verifying PDF..."
if [ -f pdf/${BASE}.pdf ]; then
    echo "   ✓ PDF generated successfully."
else
    echo "   ✗ ERROR: Build failed. Check pdf/${BASE}.log"
    exit 1
fi

echo ""
echo "4. Word count (texcount)..."
if [ -f ../texcount.pl ]; then
    ABSTRACT=$(sed -n '/\\begin{abstract}/,/\\end{abstract}/p' ${BASE}.tex \
        | perl ../texcount.pl -brief - 2>/dev/null)
    ABSTRACT_WORDS=$(echo "$ABSTRACT" | grep -oP '^\d+')
    echo "   Abstract: ${ABSTRACT_WORDS:-?} words (limit: 200)"
    echo ""

    perl ../texcount.pl -v0 -sum ${BASE}.tex 2>/dev/null | head -7
else
    echo "   texcount.pl not found in project root — skipping."
fi

echo ""
echo "5. Generating .docx (for T&F submission)..."
if command -v pandoc &>/dev/null; then
    pandoc ${BASE}.tex \
      --bibliography=references.bib \
      --citeproc \
      --resource-path=.:figures \
      --lua-filter=number-tables.lua \
      -o ${BASE}.docx 2>&1
    if [ -f ${BASE}.docx ]; then
        SIZE=$(du -h ${BASE}.docx | cut -f1)
        echo "   ✓ ${BASE}.docx generated (${SIZE})"
    else
        echo "   ✗ docx generation failed"
    fi
else
    echo "   pandoc not found — skipping docx generation"
fi

echo ""
echo "6. Building submission zip..."
cp pdf/${BASE}.bbl ./${BASE}.bbl 2>/dev/null
rm -f ${BASE}_submission.zip
zip -q ${BASE}_submission.zip \
    ${BASE}.tex \
    references.bib \
    ${BASE}.bbl
rm -f ${BASE}.bbl
ZIP_SIZE=$(du -h ${BASE}_submission.zip | cut -f1)
echo "   ✓ ${BASE}_submission.zip (${ZIP_SIZE})"

echo ""
echo "------------------------------------------------------"
echo "✅ SUCCESS: pdf/${BASE}.pdf  |  ${BASE}.docx  |  ${BASE}_submission.zip"
echo "------------------------------------------------------"

