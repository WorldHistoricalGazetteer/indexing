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

# 00README.config: tell ArXiv/Springer-like systems to use xelatex
# (this is the same format that works on ArXiv)
cat > "$SUBMIT/00README.config" << EOF
defaultopt: xelatex
EOF

# latexmkrc: tell latexmk-based systems to use xelatex
# (include both dotted and undotted variants)
for RC in latexmkrc .latexmkrc; do
cat > "$SUBMIT/$RC" << 'EOF'
$pdf_mode = 5;
$xelatex = 'xelatex -interaction=nonstopmode %O %S';
$postscript_mode = $dvi_mode = 0;
EOF
done

# Makefile: fallback for systems that run make
cat > "$SUBMIT/Makefile" << EOF
all:
	xelatex -interaction=nonstopmode ${BASE}.tex
	bibtex ${BASE}
	xelatex -interaction=nonstopmode ${BASE}.tex
	xelatex -interaction=nonstopmode ${BASE}.tex
EOF

cp ${BASE}.tex "$SUBMIT/"
cp references.bib "$SUBMIT/"
cp pdf/${BASE}.bbl "$SUBMIT/" 2>/dev/null
cp -r fonts "$SUBMIT/" 2>/dev/null || :

(cd "$SUBMIT" && zip -qr ../${BASE}_submission.zip .)
rm -rf "$SUBMIT"
ZIP_SIZE=$(du -h ${BASE}_submission.zip | cut -f1)
echo "   ✓ ${BASE}_submission.zip (${ZIP_SIZE})"

echo ""
echo "------------------------------------------------------"
echo "✅ SUCCESS: pdf/${BASE}.pdf  |  ${BASE}.docx  |  ${BASE}_submission.zip"
echo "------------------------------------------------------"

