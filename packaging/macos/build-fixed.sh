#!/bin/bash
set -euo pipefail

# Build script that automatically handles the gdk-pixbuf hang issue
# This follows the official PyGObject deployment guide using gtk-mac-bundler

echo "Building sshPilot.app with gtk-mac-bundler..."

# Step 1: Set up environment to prevent code signing issues
echo "Setting up environment to prevent code signing conflicts..."
export BREW_PREFIX="$(brew --prefix)"

# Disable code signing to prevent hangs
export CODESIGN_ALLOCATE=""
export CODE_SIGN_IDENTITY=""
export CODESIGN_FLAGS=""

# Re-sign with ad-hoc signatures for redistribution (prevents install_name_tool issues)
echo "Preparing libraries for redistribution with ad-hoc signatures..."
codesign --force --sign - "${BREW_PREFIX}/bin/gdk-pixbuf-query-loaders" 2>/dev/null || true
find "${BREW_PREFIX}/lib" -name "*.dylib" -exec codesign --force --sign - {} \; 2>/dev/null || true

# Step 2: Build the bundle with timeout protection
echo "Building bundle..."
echo "Note: This may take several minutes. If it hangs for more than 10 minutes, press Ctrl+C and try again."

# Check if timeout command is available (macOS doesn't have it by default)
if command -v timeout >/dev/null 2>&1; then
  timeout 600 bash packaging/macos/make-bundle.sh || {
    echo "⚠️  Build timed out or failed. This often happens due to code signing issues."
    echo "Trying alternative approach..."
    
    # Kill any hanging processes
    pkill -f "gtk-mac-bundler" 2>/dev/null || true
    pkill -f "install_name_tool" 2>/dev/null || true
    
    echo "Retrying build with additional code signing disabled..."
    export CODESIGN_ALLOCATE=""
    export CODE_SIGN_IDENTITY=""
    export CODESIGN_FLAGS=""
    export MACOSX_DEPLOYMENT_TARGET="10.15"
    
    bash packaging/macos/make-bundle.sh
  }
else
  # macOS doesn't have timeout, so just run it and let user interrupt if needed
  echo "⚠️  timeout command not available on macOS - if build hangs, press Ctrl+C"
  bash packaging/macos/make-bundle.sh
fi

# Step 4: Clean up
echo "Cleaning up temporary files..."
rm -rf packaging/macos/temp-bin

echo "Build complete! The bundle is now in dist/sshPilot.app"
echo "You can now open: open dist/sshPilot.app"
