#!/bin/bash

echo "1. Cleaning previous build artifacts..."
rm -f pdf/symphonym-ijde.pdf

echo "2. Compiling with latexmk..."
latexmk -f symphonym-ijde.tex

echo "3. Verifying PDF..."
if [ -f pdf/symphonym-ijde.pdf ]; then
    echo "   ✓ PDF generated successfully."
else
    echo "   ✗ ERROR: Build failed. Check pdf/symphonym-ijde.log"
    exit 1
fi

echo ""
echo "4. Word count (texcount)..."
if [ -f ../texcount.pl ]; then
    ABSTRACT=$(sed -n '/\\begin{abstract}/,/\\end{abstract}/p' symphonym-ijde.tex \
        | perl ../texcount.pl -brief - 2>/dev/null)
    ABSTRACT_WORDS=$(echo "$ABSTRACT" | grep -oP '^\d+')
    echo "   Abstract: ${ABSTRACT_WORDS:-?} words (limit: 200)"
    echo ""

    perl ../texcount.pl -v0 -sum symphonym-ijde.tex 2>/dev/null | head -7
else
    echo "   texcount.pl not found in project root — skipping."
fi

echo ""
echo "------------------------------------------------------"
echo "✅ SUCCESS: pdf/symphonym-ijde.pdf"
echo "------------------------------------------------------"

