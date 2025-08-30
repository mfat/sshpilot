#!/bin/bash
#
# Simple launcher script for building sshPilot DMG on macOS
# This is the main entry point for building a universal DMG
#

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                   sshPilot macOS DMG Builder                 ║"
echo "║                Universal Binary (Intel + Apple Silicon)      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if we're on macOS
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo -e "${RED}❌ This script must be run on macOS${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Running on macOS${NC}"

# Check Python version
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✓ Python ${PYTHON_VERSION}${NC}"

# Check for required tools
if ! command -v brew &> /dev/null; then
    echo -e "${RED}❌ Homebrew is required but not installed${NC}"
    echo -e "${YELLOW}Please install Homebrew from: https://brew.sh${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Homebrew found${NC}"

# Show build options
echo ""
echo -e "${YELLOW}Build Options:${NC}"
echo "1. Complete build (recommended) - Handles everything automatically"
echo "2. Use Makefile - Step-by-step build with make"
echo "3. Manual build - Individual scripts"
echo ""

read -p "Choose option (1-3) [1]: " choice
choice=${choice:-1}

case $choice in
    1)
        echo -e "${BLUE}Starting complete build...${NC}"
        python3 build_complete.py
        ;;
    2)
        echo -e "${BLUE}Using Makefile build...${NC}"
        make all
        ;;
    3)
        echo -e "${BLUE}Manual build process...${NC}"
        echo "Run these commands in order:"
        echo "  python3 build_macos.py"
        echo "  python3 create_styled_dmg.py"
        echo "  python3 -c \"from build_complete import UniversalDMGBuilder; UniversalDMGBuilder().verify_dmg()\""
        exit 0
        ;;
    *)
        echo -e "${RED}Invalid option${NC}"
        exit 1
        ;;
esac

# Check if build was successful
if [[ -f "dist/sshPilot-1.0.0-universal.dmg" ]]; then
    echo ""
    echo -e "${GREEN}"
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                    🎉 BUILD SUCCESSFUL! 🎉                  ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "${GREEN}✅ DMG created: dist/sshPilot-1.0.0-universal.dmg${NC}"
    echo -e "${GREEN}✅ Supports: Intel x86_64 + Apple Silicon arm64${NC}"
    echo ""
    echo -e "${YELLOW}To test the DMG:${NC}"
    echo "1. Double-click the DMG to mount it"
    echo "2. Drag sshPilot.app to the Applications folder"
    echo "3. Launch sshPilot from Applications or Spotlight"
    echo ""
    echo -e "${YELLOW}File size: $(du -h dist/sshPilot-1.0.0-universal.dmg | cut -f1)${NC}"
    
    # Ask if user wants to open the DMG
    echo ""
    read -p "Open the DMG now? (y/n) [y]: " open_dmg
    open_dmg=${open_dmg:-y}
    
    if [[ "$open_dmg" == "y" || "$open_dmg" == "Y" ]]; then
        open "dist/sshPilot-1.0.0-universal.dmg"
    fi
else
    echo -e "${RED}"
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                     ❌ BUILD FAILED ❌                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "${RED}DMG file was not created. Check the build output above.${NC}"
    exit 1
fi