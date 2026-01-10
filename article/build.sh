#!/bin/bash

# Stop the script if any command fails
set -e

# ----------------------
# PART 1: COMPILATION
# ----------------------

echo "1. Creating output directory..."
mkdir -p pdf

echo "2. Running XeLaTeX (Pass 1)..."
xelatex -file-line-error -interaction=nonstopmode -output-directory=pdf symphonym.tex

echo "3. Running BibTeX..."
# We point bibtex to the .aux file inside the pdf folder
bibtex pdf/symphonym

echo "4. Running XeLaTeX (Pass 2)..."
xelatex -file-line-error -interaction=nonstopmode -output-directory=pdf symphonym.tex

echo "5. Running XeLaTeX (Pass 3)..."
xelatex -file-line-error -interaction=nonstopmode -output-directory=pdf symphonym.tex

echo "6. Running XeLaTeX (Pass 4 - Final)..."
xelatex -file-line-error -interaction=nonstopmode -output-directory=pdf symphonym.tex

echo "Build complete! Output: article/pdf/symphonym.pdf"

# ----------------------
# PART 2: ARXIV BUNDLING
# ----------------------

echo "6. Packaging for arXiv..."

SUBMISSION_DIR="arxiv_submission"

# Clean up
rm -rf $SUBMISSION_DIR
rm -f arxiv_submission.zip
mkdir -p $SUBMISSION_DIR

# Copy Files
cp symphonym.tex $SUBMISSION_DIR/
cp pdf/symphonym.bbl $SUBMISSION_DIR/
cp *.cls *.sty *.bst $SUBMISSION_DIR/ 2>/dev/null || :
cp *.png *.jpg *.jpeg *.pdf $SUBMISSION_DIR/ 2>/dev/null || :

# Copy the FONTS folder
cp -r fonts $SUBMISSION_DIR/

# Remove the compiled PDF if it got copied by mistake
rm -f $SUBMISSION_DIR/symphonym.pdf

# --- CHANGED SECTION ---
# Enter the directory and zip the CONTENTS, not the directory itself
cd $SUBMISSION_DIR
zip -r ../arxiv_submission.zip .
cd ..
# -----------------------

rm -rf $SUBMISSION_DIR

echo "------------------------------------------------------"
echo "✅ ZIP Created: article/arxiv_submission.zip"
echo "------------------------------------------------------"