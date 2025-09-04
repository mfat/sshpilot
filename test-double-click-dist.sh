#!/bin/bash

# Test script to simulate double-click environment for dist bundle
# This helps debug issues that only occur when launching via GUI

echo "Testing dist app bundle in double-click simulation..."
echo "Bundle: ./dist/sshPilot.app"

# Check if bundle exists
if [ ! -d "./dist/sshPilot.app" ]; then
    echo "Error: ./dist/sshPilot.app not found"
    exit 1
fi

echo "Simulating double-click environment..."
echo "Minimal PATH: /usr/bin:/bin"

# Simulate the minimal environment that double-click provides
# This is the key difference - double-click doesn't inherit your shell's PATH
export PATH="/usr/bin:/bin"
export HOME="$HOME"
export USER="$USER"

echo "Launching app..."
# Test the app with minimal environment
./dist/sshPilot.app/Contents/MacOS/sshPilot --help

if [ $? -eq 0 ]; then
    echo "✓ App launched successfully in double-click simulation"
else
    echo "✗ App failed to launch in double-click simulation"
    exit 1
fi

echo "Double-click simulation test completed successfully!"
