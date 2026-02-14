#!/bin/bash

# Install Flite and lex_lookup for English G2P support in Epitran
# Based on: https://github.com/dmort27/epitran
#
# This version works around the flite_voice_list.c build error by:
# 1. Building only the libraries and testsuite (not main/)
# 2. Installing to ~/.local

set -e

echo "=================================================="
echo "Installing Flite for English G2P"
echo "=================================================="

# Check if lex_lookup already installed
if command -v lex_lookup >/dev/null 2>&1; then
    echo "✓ lex_lookup already installed: $(which lex_lookup)"
    echo ""
    echo "If you still get lex_lookup warnings, you may need to reinstall."
    read -p "Continue with reinstall? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 0
    fi
fi

# Ensure ~/.local/bin exists
mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.local/lib"
mkdir -p "$HOME/.local/include"

# Create temporary build directory
BUILD_DIR=$(mktemp -d)
echo "Building in: $BUILD_DIR"
cd "$BUILD_DIR"

# Clone Flite repository
echo ""
echo "Cloning Flite repository..."
git clone https://github.com/festvox/flite.git
cd flite

# Configure
echo ""
echo "Configuring Flite..."
./configure --prefix="$HOME/.local" --with-langvox=built

echo ""
echo "Building Flite libraries (skipping broken main/)..."
# Build only what we need, avoiding the broken main/ directory
make -j$(nproc) -C src || true
make -j$(nproc) -C lang || true
make -j$(nproc) -C lib || true

# Install libraries manually
echo ""
echo "Installing Flite libraries..."
cp -r include/* "$HOME/.local/include/" 2>/dev/null || true
find build -name "*.a" -exec cp {} "$HOME/.local/lib/" \; 2>/dev/null || true
find build -name "*.so*" -exec cp {} "$HOME/.local/lib/" \; 2>/dev/null || true

# Now build lex_lookup from testsuite
echo ""
echo "Building lex_lookup tool..."
cd testsuite

# Create a simple Makefile for lex_lookup if it doesn't work
if ! make lex_lookup 2>/dev/null; then
    echo "Standard build failed, trying manual compilation..."

    # Find the compiler used
    CC="${CC:-gcc}"
    if [ -n "$CONDA_PREFIX" ]; then
        CC=$(find "$CONDA_PREFIX/bin" -name "*-gcc" | head -1)
        [ -z "$CC" ] && CC="gcc"
    fi

    # Manual compile
    $CC -I../include -L../build/*/lib \
        lex_lookup.c \
        -o lex_lookup \
        -lflite_cmulex -lflite_usenglish -lflite -lm || {
        echo "✗ Failed to build lex_lookup"
        echo ""
        echo "Trying alternative: using pre-built Flite binary..."
        cd /
        rm -rf "$BUILD_DIR"

        # Try downloading pre-built binary
        echo "Checking for system flite installation..."
        if command -v flite >/dev/null 2>&1; then
            # Check if we can find lex_lookup in system paths
            for bindir in /usr/bin /usr/local/bin /opt/*/bin; do
                if [ -f "$bindir/lex_lookup" ]; then
                    echo "✓ Found system lex_lookup at $bindir/lex_lookup"
                    echo "Copying to ~/.local/bin..."
                    cp "$bindir/lex_lookup" "$HOME/.local/bin/"
                    chmod +x "$HOME/.local/bin/lex_lookup"
                    exit 0
                fi
            done
        fi

        echo "✗ Could not build or find lex_lookup"
        echo ""
        echo "Options:"
        echo "1. Ask your system administrator to install 'flite' package"
        echo "2. Try building on a different machine and copying the binary"
        echo "3. For now, Epitran will work but English G2P will fall back to rule-based"
        echo ""
        echo "The rebuild will continue without English lex_lookup support."
        exit 0
    }
fi

echo ""
echo "Installing lex_lookup to $HOME/.local/bin..."
cp lex_lookup "$HOME/.local/bin/"
chmod +x "$HOME/.local/bin/lex_lookup"

# Clean up
cd /
rm -rf "$BUILD_DIR"

echo ""
echo "=================================================="
echo "✓ Flite installation complete!"
echo "=================================================="
echo ""
echo "Installed files:"
echo "  lex_lookup: $HOME/.local/bin/lex_lookup"
echo "  libraries:  $HOME/.local/lib/libflite*.a"
echo "  headers:    $HOME/.local/include/flite/"
echo ""

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "⚠ WARNING: $HOME/.local/bin is not in your PATH"
    echo ""
    echo "Add this to your ~/.bashrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "Then run:"
    echo "  source ~/.bashrc"
else
    echo "✓ $HOME/.local/bin is already in your PATH"
fi

echo ""
echo "Test installation:"
echo "  echo 'test' | lex_lookup"
echo ""
echo "After installation, restart your rebuild job to pick up lex_lookup"
echo ""

