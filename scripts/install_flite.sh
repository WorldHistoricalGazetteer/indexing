#!/bin/bash

# Install Flite and lex_lookup for English G2P support in Epitran
# Based on: https://github.com/dmort27/epitran

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

# Create temporary build directory
BUILD_DIR=$(mktemp -d)
echo "Building in: $BUILD_DIR"
cd "$BUILD_DIR"

# Clone Flite repository
echo ""
echo "Cloning Flite repository..."
git clone https://github.com/festvox/flite.git
cd flite

# Configure and build (install to ~/.local)
echo ""
echo "Configuring Flite..."
./configure --prefix="$HOME/.local"

echo ""
echo "Building Flite (this may take a few minutes)..."
make -j$(nproc)

echo ""
echo "Installing Flite to $HOME/.local..."
make install

# Build lex_lookup specifically
echo ""
echo "Building lex_lookup tool..."
cd testsuite
make lex_lookup

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
echo "Installed binaries:"
echo "  flite:      $HOME/.local/bin/flite"
echo "  lex_lookup: $HOME/.local/bin/lex_lookup"
echo ""

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "⚠ WARNING: $HOME/.local/bin is not in your PATH"
    echo ""
    echo "Add this to your ~/.bashrc or ~/.bash_profile:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "Then run:"
    echo "  source ~/.bashrc"
else
    echo "✓ $HOME/.local/bin is already in your PATH"
fi

echo ""
echo "Test installation:"
echo "  flite -t 'Hello world'"
echo "  echo 'test' | lex_lookup"
echo ""

