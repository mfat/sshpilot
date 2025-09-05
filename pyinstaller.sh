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

# Check if virtual environment exists
if [ ! -d ".venv-homebrew" ]; then
    echo "❌ Error: .venv-homebrew virtual environment not found."
    echo "Please ensure the Homebrew virtual environment is set up."
    exit 1
fi

# Activate virtual environment and build
echo "📦 Activating Homebrew virtual environment..."
source .venv-homebrew/bin/activate

echo "🔨 Running PyInstaller..."
python -m PyInstaller --clean --noconfirm sshpilot.spec

# Check if build was successful
if [ -d "dist/SSHPilot.app" ]; then
    echo "✅ Build successful! Bundle created at: dist/SSHPilot.app"
    
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
    
    echo "DEBUG: Detected version: $VERSION"
    DMG_NAME="sshPilot-${VERSION}.dmg"
    DMG_PATH="dist/${DMG_NAME}"
    echo "DEBUG: DMG will be created as: $DMG_PATH"
    
    # Remove existing DMG if it exists
    if [ -f "$DMG_PATH" ]; then
        rm "$DMG_PATH"
    fi

    # Create DMG using create-dmg
    echo "🎨 Creating DMG with create-dmg..."
    create-dmg \
        --volname "sshPilot" \
        --volicon "packaging/macos/sshpilot.icns" \
        --window-pos 200 120 \
        --window-size 800 400 \
        --icon-size 100 \
        --icon "SSHPilot.app" 200 190 \
        --hide-extension "SSHPilot.app" \
        --app-drop-link 600 185 \
        "$DMG_PATH" \
        "dist/SSHPilot.app"
    
    if [ $? -eq 0 ]; then
        echo "✅ DMG created successfully: $DMG_PATH"
    else
        echo "❌ Failed to create DMG with create-dmg"
        echo "⚠️  DMG creation failed, but bundle was created successfully."
        echo ""
        echo "🎉 SSHPilot bundle is ready!"
        echo "📍 Location: $(pwd)/dist/SSHPilot.app"
        echo "🚀 You can now run: open dist/SSHPilot.app"
        exit 0
    fi
    
    if [ $? -eq 0 ] && [ -f "$DMG_PATH" ]; then
        echo "✅ DMG created successfully!"
        echo ""
        echo "🎉 SSHPilot bundle and DMG are ready!"
        echo "📍 Bundle: $(pwd)/dist/SSHPilot.app"
        echo "📍 DMG: $(pwd)/$DMG_PATH"
        echo "🚀 You can now run: open dist/SSHPilot.app"
        echo "📁 Or mount the DMG: open $DMG_PATH"
    else
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
