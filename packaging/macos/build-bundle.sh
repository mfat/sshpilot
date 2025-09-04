#!/bin/bash

# Build script for sshPilot macOS app bundle
# Following PyGObject deployment guide using gtk-mac-bundler

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGING_DIR="$(dirname "${BASH_SOURCE[0]}")"
BUILD_DIR="$PACKAGING_DIR/build"
BUNDLE_NAME="sshPilot.app"
BUNDLE_PATH="$BUILD_DIR/$BUNDLE_NAME"

echo -e "${GREEN}Building sshPilot macOS app bundle...${NC}"

# Clean previous build
if [ -d "$BUILD_DIR" ]; then
    echo -e "${YELLOW}Cleaning previous build...${NC}"
    rm -rf "$BUILD_DIR"
fi

mkdir -p "$BUILD_DIR"

# Check for required tools
echo -e "${GREEN}Checking dependencies...${NC}"

# Check for gtk-mac-bundler
if ! command -v gtk-mac-bundler &> /dev/null; then
    echo -e "${RED}Error: gtk-mac-bundler not found. Please install it first.${NC}"
    echo "Install with: brew install gtk-mac-bundler"
    exit 1
fi

# Check for Homebrew dependencies
REQUIRED_BREW_PACKAGES=(
    "gtk4"
    "libadwaita" 
    "pygobject3"
    "py3cairo"
    "vte3"
    "gobject-introspection"
    "adwaita-icon-theme"
    "pkg-config"
    "glib"
    "graphene"
    "icu4c"
    "sshpass"
)

echo -e "${GREEN}Checking Homebrew packages...${NC}"
for package in "${REQUIRED_BREW_PACKAGES[@]}"; do
    if ! brew list "$package" &> /dev/null; then
        echo -e "${RED}Error: $package not installed. Please install with: brew install $package${NC}"
        exit 1
    fi
done

# Create virtual environment for the bundle
echo -e "${GREEN}Creating Python virtual environment...${NC}"
cd "$BUILD_DIR"
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
echo -e "${GREEN}Installing Python dependencies...${NC}"
pip install --upgrade pip
pip install -r "$PROJECT_ROOT/requirements.txt"

# Copy application files
echo -e "${GREEN}Copying application files...${NC}"
cp -r "$PROJECT_ROOT/sshpilot" venv/lib/python*/site-packages/
cp "$PROJECT_ROOT/run.py" venv/

# Create the app bundle using gtk-mac-bundler
echo -e "${GREEN}Creating app bundle with gtk-mac-bundler...${NC}"
cd "$PACKAGING_DIR"

# Set environment variables for bundling
export PYTHONPATH="$BUILD_DIR/venv/lib/python*/site-packages:$PYTHONPATH"
export GI_TYPELIB_PATH="/opt/homebrew/lib/girepository-1.0:/usr/local/lib/girepository-1.0:$GI_TYPELIB_PATH"
export PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig:/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH"

# Run gtk-mac-bundler
gtk-mac-bundler bundle.ini \
    --verbose \
    --python-venv="$BUILD_DIR/venv" \
    --python-site-packages \
    --python-module-path="$BUILD_DIR/venv/lib/python*/site-packages" \
    --python-module-path="$PROJECT_ROOT/sshpilot" \
    --main-program="$BUILD_DIR/venv/run.py" \
    --output-dir="$BUILD_DIR"

# Verify bundle was created
if [ ! -d "$BUNDLE_PATH" ]; then
    echo -e "${RED}Error: App bundle was not created successfully${NC}"
    exit 1
fi

echo -e "${GREEN}App bundle created successfully at: $BUNDLE_PATH${NC}"

# Run tests
echo -e "${GREEN}Running bundle tests...${NC}"
"$PACKAGING_DIR/test-bundle.sh" "$BUNDLE_PATH"

echo -e "${GREEN}Build completed successfully!${NC}"
echo -e "${YELLOW}App bundle location: $BUNDLE_PATH${NC}"
