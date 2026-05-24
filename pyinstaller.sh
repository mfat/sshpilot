#!/bin/bash

# Build script for SSHPilot PyInstaller bundle
# This script activates the Homebrew virtual environment and builds the bundle

set -e  # Exit on any error

echo "🚀 Building SSHPilot PyInstaller bundle..."

# Check if we're in the right directory
if [ ! -f "sshpilot.spec" ]; then
    echo "❌ Error: sshpilot.spec not found. Please run this script from the project root directory."
    exit 1
fi

# Check if virtual environment exists, create if not
if [ ! -d ".venv-homebrew" ]; then
    echo "📦 Creating Homebrew virtual environment..."
    
    # Detect architecture and set Homebrew path
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ]; then
        # Apple Silicon Mac
        HOMEBREW_PREFIX="/opt/homebrew"
        echo "🍎 Detected Apple Silicon Mac (ARM64)"
    else
        # Intel Mac
        HOMEBREW_PREFIX="/usr/local"
        echo "💻 Detected Intel Mac (x86_64)"
    fi
    
    # Check if Homebrew Python is available
    PYTHON_PATH="$HOMEBREW_PREFIX/opt/python@3.13/bin/python3.13"
    if [ ! -f "$PYTHON_PATH" ]; then
        echo "❌ Homebrew Python 3.13 not found at $PYTHON_PATH"
        echo "Please install it with:"
        echo "   brew install python@3.13"
        exit 1
    fi
    
    echo "🐍 Using Python from: $PYTHON_PATH"

    # Create virtual environment using Homebrew Python
    "$PYTHON_PATH" -m venv .venv-homebrew
    echo "✅ Virtual environment created successfully"

    # Activate and install PyInstaller
    echo "📦 Installing PyInstaller..."
    source .venv-homebrew/bin/activate
    pip install PyInstaller
    echo "✅ PyInstaller installed successfully"
else
    echo "📦 Activating existing Homebrew virtual environment..."
    source .venv-homebrew/bin/activate
fi

echo "🔨 Running PyInstaller..."
python -m PyInstaller --clean --noconfirm sshpilot.spec

# Check if build was successful
if [ -d "dist/SSHPilot.app" ]; then
    echo "✅ Build successful! Bundle created at: dist/SSHPilot.app"

    # Bundle sshpass into the correct location expected by hook-gtk_runtime.py
    # (Contents/Resources/bin/ — NOT Contents/MacOS/ which PyInstaller's binaries[] uses)
    echo "🔑 Bundling sshpass..."
    SSHPASS_SRC="$(command -v sshpass 2>/dev/null || true)"
    if [ -n "$SSHPASS_SRC" ] && [ -f "$SSHPASS_SRC" ]; then
        SSHPASS_DEST_DIR="dist/SSHPilot.app/Contents/Resources/bin"
        mkdir -p "$SSHPASS_DEST_DIR"
        cp "$SSHPASS_SRC" "$SSHPASS_DEST_DIR/sshpass"
        chmod +x "$SSHPASS_DEST_DIR/sshpass"
        echo "✅ sshpass bundled at: $SSHPASS_DEST_DIR/sshpass"
    else
        echo "⚠️  sshpass not found in PATH; bundle will require system sshpass"
    fi

    # Ad-hoc sign the app bundle — prevents "damaged app" Gatekeeper error when downloaded
    echo "🔐 Ad-hoc code signing app bundle..."
    codesign --sign - --force --deep --timestamp=none "dist/SSHPilot.app"
    codesign --verify --verbose "dist/SSHPilot.app" 2>&1 || true
    echo "✅ App bundle signed (ad-hoc)"

    # Create DMG file using create-dmg
    echo "📦 Creating DMG file..."
    
    # Check if create-dmg is installed
    if ! command -v create-dmg &> /dev/null; then
        echo "❌ create-dmg is not installed. Please install it with:"
        echo "   brew install create-dmg"
        echo ""
        echo "🎉 SSHPilot bundle is ready!"
        echo "📍 Location: $(pwd)/dist/SSHPilot.app"
        echo "🚀 You can now run: open dist/SSHPilot.app"
        exit 0
    fi
    
    # Read version from __init__.py
    VERSION=$(grep -o '__version__ = "[^"]*"' sshpilot/__init__.py | cut -d'"' -f2)
    if [ -z "$VERSION" ]; then
        echo "⚠️  Could not read version from sshpilot/__init__.py, using date instead"
        VERSION=$(date +%Y%m%d)
    fi
    
    # Detect architecture for DMG naming
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ]; then
        ARCH_NAME="aarch64"
    else
        ARCH_NAME="x86_64"
    fi
    
    echo "DEBUG: Detected version: $VERSION"
    echo "DEBUG: Detected architecture: $ARCH ($ARCH_NAME)"
    DMG_NAME="sshPilot-macOS-${VERSION}-${ARCH_NAME}.dmg"
    DMG_PATH="dist/${DMG_NAME}"
    echo "DEBUG: DMG will be created as: $DMG_PATH"
    
    # Remove existing DMG if it exists
    if [ -f "$DMG_PATH" ]; then
        rm "$DMG_PATH"
    fi

    # Create DMG using create-dmg
    echo "🎨 Creating DMG with create-dmg..."
    if create-dmg \
        --volname "sshPilot" \
        --volicon "packaging/macos/sshpilot.icns" \
        --window-pos 200 120 \
        --window-size 800 400 \
        --icon-size 100 \
        --icon "SSHPilot.app" 200 190 \
        --hide-extension "SSHPilot.app" \
        --app-drop-link 600 185 \
        --skip-jenkins \
        "$DMG_PATH" \
        "dist/SSHPilot.app"; then
        if [ -f "$DMG_PATH" ]; then
            echo "✅ DMG created successfully!"
            echo ""
            echo "🎉 SSHPilot bundle and DMG are ready!"
            echo "📍 Bundle: $(pwd)/dist/SSHPilot.app"
            echo "📍 DMG: $(pwd)/$DMG_PATH"
            echo "🚀 You can now run: open dist/SSHPilot.app"
            echo "📁 Or mount the DMG: open $DMG_PATH"
        else
            echo "⚠️ DMG command succeeded, but file not found."
            echo ""
            echo "🎉 SSHPilot bundle is ready!"
            echo "📍 Location: $(pwd)/dist/SSHPilot.app"
            echo "🚀 You can now run: open dist/SSHPilot.app"
        fi
    else
        echo "❌ Failed to create DMG with create-dmg"
        echo "⚠️  DMG creation failed, but bundle was created successfully."
        echo ""
        echo "🎉 SSHPilot bundle is ready!"
        echo "📍 Location: $(pwd)/dist/SSHPilot.app"
        echo "🚀 You can now run: open dist/SSHPilot.app"
    fi
else
    echo "❌ Build failed! Bundle not found at dist/SSHPilot.app"
    exit 1
fi
