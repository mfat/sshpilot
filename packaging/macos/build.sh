#!/bin/bash

# Main build script for sshPilot macOS app bundle
# Provides options for different build methods

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PACKAGING_DIR="$(dirname "${BASH_SOURCE[0]}")"

echo -e "${GREEN}sshPilot macOS App Bundle Builder${NC}"
echo "=================================="

# Show usage if no arguments
if [ $# -eq 0 ]; then
    echo -e "${YELLOW}Usage: $0 [method]${NC}"
    echo ""
    echo "Available build methods:"
    echo "  simple     - Quick build using Homebrew packages (requires Homebrew on target)"
    echo "  standalone - Self-contained bundle with all dependencies copied"
    echo "  gtk-osx    - Full gtk-osx build (most compatible, takes hours)"
    echo "  test       - Test an existing bundle"
    echo ""
    echo "Examples:"
    echo "  $0 simple      # Quick development build"
    echo "  $0 standalone  # Production-ready standalone bundle"
    echo "  $0 test        # Test existing bundle"
    exit 1
fi

METHOD="$1"

case "$METHOD" in
    "simple")
        echo -e "${GREEN}Building with simple method (requires Homebrew on target)...${NC}"
        "$PACKAGING_DIR/build-final.sh"
        ;;
    "standalone")
        echo -e "${GREEN}Building standalone bundle (self-contained)...${NC}"
        "$PACKAGING_DIR/build-bundle-standalone.sh"
        ;;
    "gtk-osx")
        echo -e "${GREEN}Building with gtk-osx method (full build)...${NC}"
        echo -e "${YELLOW}Note: This will take several hours to complete.${NC}"
        echo -e "${YELLOW}Make sure you have run setup-gtk-osx.sh first.${NC}"
        read -p "Continue? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            "$PACKAGING_DIR/build-bundle.sh"
        else
            echo -e "${YELLOW}Build cancelled.${NC}"
            exit 0
        fi
        ;;
    "test")
        echo -e "${GREEN}Testing existing bundle...${NC}"
        BUNDLE_PATH="${2:-./build/sshPilot.app}"
        if [ ! -d "$BUNDLE_PATH" ]; then
            echo -e "${RED}Error: Bundle not found at $BUNDLE_PATH${NC}"
            echo "Usage: $0 test [path-to-bundle]"
            exit 1
        fi
        "$PACKAGING_DIR/test-bundle.sh" "$BUNDLE_PATH"
        ;;
    "setup")
        echo -e "${GREEN}Setting up gtk-osx environment...${NC}"
        "$PACKAGING_DIR/setup-gtk-osx.sh"
        ;;
    *)
        echo -e "${RED}Error: Unknown method '$METHOD'${NC}"
        echo "Available methods: simple, standalone, gtk-osx, test, setup"
        exit 1
        ;;
esac

echo -e "\n${GREEN}Build process completed!${NC}"
