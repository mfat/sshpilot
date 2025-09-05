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
    echo ""
    echo "🎉 SSHPilot bundle is ready!"
    echo "📍 Location: $(pwd)/dist/SSHPilot.app"
    echo "🚀 You can now run: open dist/SSHPilot.app"
else
    echo "❌ Build failed! Bundle not found at dist/SSHPilot.app"
    exit 1
fi
