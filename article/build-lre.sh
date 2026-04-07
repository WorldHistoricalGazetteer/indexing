#!/bin/bash

# Usage: bash build-lre.sh
# Builds symphonym-lre for Language Resources and Evaluation (Springer) submission

BASE="symphonym-lre"
echo "Building LRE version: ${BASE}.tex"

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
    echo "   Abstract: ${ABSTRACT_WORDS:-?} words"
    echo ""

    perl ../texcount.pl -v0 -sum ${BASE}.tex 2>/dev/null | head -7
else
    echo "   texcount.pl not found in project root — skipping."
fi

echo ""
echo "5. Generating .docx..."
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
SUBMIT="${BASE}_submission"
rm -rf "$SUBMIT" ${BASE}_submission.zip
mkdir -p "$SUBMIT"

# EM caches compilation state per filename — use a distinct name to force
# a clean XeLaTeX build (per EM guide: "save your .tex file using a
# different file name from the original").
EM_BASE="${BASE}-r1"

# Copy .tex with EM-compatible tweaks:
#   1. XeLaTeX directive on line 1 (EM requires "%!TEX TS-program" variant)
#   2. Rewrite Path=fonts/ → Path=./ since fonts are flattened into zip root
sed -e '1s/^.*TEX.*program.*$/%!TEX TS-program = xelatex/' \
    -e 's|Path=fonts/|Path=./|g' \
    ${BASE}.tex > "$SUBMIT/${EM_BASE}.tex"

cp references.bib "$SUBMIT/"
# .bbl basename must match .tex basename for LaTeX to find it
cp pdf/${BASE}.bbl "$SUBMIT/${EM_BASE}.bbl" 2>/dev/null

# Flatten fonts into submission root (EM cannot handle subdirectories)
if [ -d fonts ]; then
    cp fonts/*.{otf,ttf,ttc,OTF,TTF,TTC} "$SUBMIT/" 2>/dev/null || :
    echo "   Flattened $(ls "$SUBMIT/"*.{otf,ttf,ttc,OTF,TTF,TTC} 2>/dev/null | wc -l) font files"
fi

# Build zip with EM-required file order: .tex first, then bib/bbl, then fonts
(cd "$SUBMIT" && \
    zip -q ../${BASE}_submission.zip ${EM_BASE}.tex && \
    zip -q ../${BASE}_submission.zip ${EM_BASE}.bbl references.bib 2>/dev/null; \
    zip -q ../${BASE}_submission.zip *.otf *.ttf *.ttc *.OTF *.TTF *.TTC 2>/dev/null; \
    true)
rm -rf "$SUBMIT"
ZIP_SIZE=$(du -h ${BASE}_submission.zip | cut -f1)
echo "   ✓ ${BASE}_submission.zip (${ZIP_SIZE})"

echo ""
echo "------------------------------------------------------"
echo "✅ SUCCESS: pdf/${BASE}.pdf  |  ${BASE}.docx  |  ${BASE}_submission.zip"
echo "------------------------------------------------------"

