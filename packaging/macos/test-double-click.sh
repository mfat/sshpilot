#!/bin/bash

# Test script to simulate double-clicking the app bundle
# This script runs the app in a minimal environment similar to double-clicking

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

BUNDLE_PATH="${1:-./build/sshPilot.app}"

if [ ! -d "$BUNDLE_PATH" ]; then
    echo -e "${RED}Error: App bundle not found at $BUNDLE_PATH${NC}"
    echo "Usage: $0 [path-to-app-bundle]"
    exit 1
fi

echo -e "${GREEN}Testing app bundle in double-click simulation...${NC}"
echo -e "${BLUE}Bundle: $BUNDLE_PATH${NC}"

# Simulate double-click environment (minimal PATH, no shell profile)
echo -e "${YELLOW}Simulating double-click environment...${NC}"

# Create a minimal environment
export PATH="/usr/bin:/bin"
export HOME="$HOME"
export USER="$USER"

# Clear other environment variables that might interfere
unset PYTHONPATH
unset GI_TYPELIB_PATH
unset PKG_CONFIG_PATH
unset LD_LIBRARY_PATH
unset DYLD_LIBRARY_PATH
unset XDG_DATA_DIRS

echo -e "${BLUE}Minimal PATH: $PATH${NC}"

# Test the app
echo -e "${GREEN}Launching app...${NC}"
"$BUNDLE_PATH/Contents/MacOS/sshPilot" --help

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ App launched successfully in double-click simulation${NC}"
else
    echo -e "${RED}✗ App failed to launch in double-click simulation${NC}"
    exit 1
fi

echo -e "${GREEN}Double-click simulation test completed successfully!${NC}"
