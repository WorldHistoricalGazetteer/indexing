#!/bin/bash

# Install Flite for English G2P support in Epitran
# Based on: https://github.com/dmort27/epitran?tab=readme-ov-file#installation-of-flite-for-english-g2p

set -e

echo "=================================================="
echo "Installing Flite for English G2P"
echo "=================================================="

# Check if already installed
if command -v flite >/dev/null 2>&1; then
    echo "✓ Flite already installed: $(which flite)"
    flite --version 2>&1 || echo "(version info not available)"
    echo ""
    echo "If you still get lex_lookup warnings, you may need to reinstall."
    read -p "Continue with reinstall? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 0
    fi
fi

# Create temporary build directory
BUILD_DIR=$(mktemp -d)
echo "Building in: $BUILD_DIR"
cd "$BUILD_DIR"

# Clone Flite repository and checkout stable release
echo ""
echo "Cloning Flite repository..."
git clone https://github.com/festvox/flite.git
cd flite

# Use the latest stable release (v2.2)
echo ""
echo "Checking out stable release v2.2..."
git checkout v2.2

# Build and install
echo ""
echo "Configuring Flite..."
./configure --prefix="$HOME/.local"

echo ""
echo "Building Flite (this may take a few minutes)..."
make -j$(nproc)

echo ""
echo "Installing Flite to $HOME/.local..."
make install

# Clean up
cd /
rm -rf "$BUILD_DIR"

echo ""
echo "=================================================="
echo "✓ Flite installation complete!"
echo "=================================================="
echo ""
echo "Flite installed to: $HOME/.local/bin/flite"
echo ""
echo "Add to your PATH if not already present:"
echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
echo ""
echo "Test installation:"
echo "  flite --version"
echo "  flite -t 'Hello world'"
echo ""

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "⚠ WARNING: $HOME/.local/bin is not in your PATH"
    echo "Add this to your ~/.bashrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi

