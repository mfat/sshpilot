#!/bin/bash
set -euo pipefail

echo "Building sshPilot.app with PyInstaller..."

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build/ dist/ __pycache__/ *.spec

# Build with PyInstaller
echo "Building with PyInstaller..."
pyinstaller sshpilot.spec

# Check if build was successful
if [ -d "dist/sshPilot.app" ]; then
    echo "✅ Build successful! sshPilot.app created in dist/"
    echo ""
    echo "Testing the app..."
    
    # Test if the app launches
    if timeout 10s open dist/sshPilot.app; then
        echo "✅ App launches successfully!"
        echo ""
        echo "🎉 sshPilot.app is ready in dist/"
        echo "You can now:"
        echo "  - Double-click dist/sshPilot.app to test"
        echo "  - Copy to Applications: cp -R dist/sshPilot.app /Applications/"
        echo "  - Run from terminal: open dist/sshPilot.app"
    else
        echo "⚠️  App may have launch issues - check Console.app for errors"
    fi
else
    echo "❌ Build failed! Check the output above for errors."
    exit 1
fi
