#!/bin/bash

# We don't use 'set -e' here because XeLaTeX warnings
# return a non-zero exit code that would stop the script.

echo "1. Cleaning previous build artifacts..."
rm -rf pdf arxiv_submission arxiv_submission.zip
mkdir -p pdf

echo "2. Compiling with latexmk..."
# -f forces completion even with the bidi warnings
latexmk -f symphonym.tex

echo "3. Verifying PDF and BBL..."
if [ -f pdf/symphonym.pdf ] && [ -f pdf/symphonym.bbl ]; then
    echo "   ✓ PDF generated successfully."
    echo "   ✓ Bibliography (.bbl) found."
else
    echo "   ✗ ERROR: Build failed to produce PDF or BBL. Check pdf/symphonym.log"
    exit 1
fi

echo "4. Packaging for arXiv..."
SUBMIT="arxiv_submission"
mkdir -p $SUBMIT

# REQUIRED FOR ARXIVE XELATEX
echo "defaultopt: xelatex" > $SUBMIT/00README.config

# Copy main files (ArXiv needs .bbl in the same folder as .tex)
cp symphonym.tex $SUBMIT/
cp pdf/symphonym.bbl $SUBMIT/

# Copy all dependencies
# Using 2>/dev/null to ignore errors if specific extensions don't exist
cp *.cls *.sty *.bst $SUBMIT/ 2>/dev/null || :
cp -r fonts $SUBMIT/ 2>/dev/null || :
cp *.png *.jpg *.jpeg *.pdf $SUBMIT/ 2>/dev/null || :

# Remove the compiled main PDF from the ZIP (arXiv compiles its own)
rm -f $SUBMIT/symphonym.pdf

echo "5. Creating ZIP..."
(cd $SUBMIT && zip -r ../arxiv_submission.zip .)

# Optional: clean up the temporary folder
# rm -rf $SUBMIT

echo "------------------------------------------------------"
echo "✅ SUCCESS: arxiv_submission.zip created."
echo "------------------------------------------------------"

echo ""
echo "6. Word count (texcount)..."
if [ -f ../texcount.pl ]; then
    # Abstract word count
    ABSTRACT=$(sed -n '/\\begin{abstract}/,/\\end{abstract}/p' symphonym.tex \
        | perl ../texcount.pl -brief - 2>/dev/null)
    ABSTRACT_WORDS=$(echo "$ABSTRACT" | grep -oP '^\d+')
    echo "   Abstract: ${ABSTRACT_WORDS:-?} words"
    echo ""

    # Full document summary
    perl ../texcount.pl -v0 -sum symphonym.tex 2>/dev/null | head -7
else
    echo "   texcount.pl not found in project root — skipping."
fi
