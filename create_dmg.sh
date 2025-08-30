#!/bin/bash
# 
# DMG creation script for sshPilot
# Creates a universal DMG that works on all macOS architectures
#

set -e

APP_NAME="sshPilot"
VERSION="1.0.0"
BUNDLE_ID="io.github.mfat.sshpilot"
DMG_NAME="${APP_NAME}-${VERSION}-universal"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

echo_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

echo_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

echo_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're on macOS
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo_error "This script must be run on macOS"
    exit 1
fi

# Check for required tools
check_tool() {
    if ! command -v "$1" &> /dev/null; then
        echo_error "$1 is required but not installed"
        return 1
    fi
    echo_success "$1 found"
    return 0
}

echo_info "Checking required tools..."
check_tool "python3" || exit 1
check_tool "iconutil" || exit 1

# Check for create-dmg, install if needed
if ! command -v create-dmg &> /dev/null; then
    echo_info "Installing create-dmg..."
    if command -v brew &> /dev/null; then
        brew install create-dmg
    else
        echo_error "create-dmg not found and Homebrew not available"
        echo_info "Please install create-dmg manually or install Homebrew"
        exit 1
    fi
fi

# Set up directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
DIST_DIR="${SCRIPT_DIR}/dist"
APP_PATH="${DIST_DIR}/${APP_NAME}.app"
DMG_PATH="${DIST_DIR}/${DMG_NAME}.dmg"

echo_info "Build directory: ${BUILD_DIR}"
echo_info "Distribution directory: ${DIST_DIR}"
echo_info "App path: ${APP_PATH}"
echo_info "DMG path: ${DMG_PATH}"

# Clean previous builds
echo_info "Cleaning previous builds..."
rm -rf "${BUILD_DIR}" "${DIST_DIR}"
rm -f "${SCRIPT_DIR}/setup.py" "${SCRIPT_DIR}/*.icns" "${SCRIPT_DIR}/*.iconset"

# Create directories
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

# Install Python dependencies
echo_info "Installing Python dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r macos_requirements.txt

# Build the application
echo_info "Building macOS application..."
python3 build_macos.py

# Verify the app was built
if [[ ! -d "${APP_PATH}" ]]; then
    echo_error "Application build failed - ${APP_PATH} not found"
    exit 1
fi

echo_success "Application built successfully"

# Verify universal binary
echo_info "Verifying universal binary..."
EXECUTABLE="${APP_PATH}/Contents/MacOS/${APP_NAME}"
if [[ -f "${EXECUTABLE}" ]]; then
    echo_info "Binary architecture info:"
    file "${EXECUTABLE}"
    lipo -info "${EXECUTABLE}" || echo_warning "Could not get lipo info"
else
    echo_warning "Executable not found at ${EXECUTABLE}"
fi

# Create DMG
echo_info "Creating DMG..."

# Remove existing DMG
rm -f "${DMG_PATH}"

# Create DMG with create-dmg
create-dmg \
    --volname "${APP_NAME} ${VERSION}" \
    --window-pos 200 120 \
    --window-size 600 400 \
    --icon-size 100 \
    --icon "${APP_NAME}.app" 175 120 \
    --hide-extension "${APP_NAME}.app" \
    --app-drop-link 425 120 \
    --format UDZO \
    "${DMG_PATH}" \
    "${DIST_DIR}/"

if [[ -f "${DMG_PATH}" ]]; then
    echo_success "DMG created successfully: ${DMG_PATH}"
    echo_info "DMG size: $(du -h "${DMG_PATH}" | cut -f1)"
    
    # Verify DMG
    echo_info "Verifying DMG..."
    hdiutil verify "${DMG_PATH}"
    echo_success "DMG verification passed"
    
    echo ""
    echo "=========================================="
    echo "BUILD COMPLETE"
    echo "=========================================="
    echo "DMG file: ${DMG_PATH}"
    echo "This DMG works on both Intel and Apple Silicon Macs"
    echo ""
    echo "To test the DMG:"
    echo "1. Double-click to mount it"
    echo "2. Drag ${APP_NAME}.app to Applications"
    echo "3. Launch from Applications folder"
    echo "=========================================="
else
    echo_error "DMG creation failed"
    exit 1
fi