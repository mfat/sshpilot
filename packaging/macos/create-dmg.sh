#!/usr/bin/env bash
set -euo pipefail

# Create a professional DMG installer for sshPilot
# This script creates a DMG with proper layout and branding

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
APP_NAME="sshPilot"
DMG_NAME="${APP_NAME}-macOS.dmg"
VOLUME_NAME="${APP_NAME}"

# Check if the app bundle exists
if [ ! -d "${DIST_DIR}/${APP_NAME}.app" ]; then
    echo "Error: ${APP_NAME}.app not found in ${DIST_DIR}" >&2
    echo "Please run make-bundle.sh first" >&2
    exit 1
fi

# Check if create-dmg is available
if ! command -v create-dmg >/dev/null 2>&1; then
    echo "Error: create-dmg not found. Install it with: brew install create-dmg" >&2
    exit 1
fi

echo "Creating professional DMG installer for ${APP_NAME}..."

# Create a temporary directory for DMG contents
TEMP_DIR="${DIST_DIR}/dmg-temp"
rm -rf "${TEMP_DIR}"
mkdir -p "${TEMP_DIR}"

# Copy the app bundle to temp directory
cp -R "${DIST_DIR}/${APP_NAME}.app" "${TEMP_DIR}/"

# Create DMG using create-dmg
echo "Building DMG with create-dmg..."

create-dmg \
    --volname "${VOLUME_NAME}" \
    --volicon "${SCRIPT_DIR}/sshPilot.icns" \
    --window-pos 200 120 \
    --window-size 600 400 \
    --icon-size 100 \
    --icon "${APP_NAME}.app" 175 120 \
    --hide-extension "${APP_NAME}.app" \
    --app-drop-link 425 120 \
    --no-internet-enable \
    "${DIST_DIR}/${DMG_NAME}" \
    "${TEMP_DIR}"

# Clean up temp directory
rm -rf "${TEMP_DIR}"

# Verify the DMG was created
if [ -f "${DIST_DIR}/${DMG_NAME}" ]; then
    echo ""
    echo "🎉 Successfully created ${DMG_NAME}!"
    echo "📁 Location: ${DIST_DIR}/${DMG_NAME}"
    echo "📊 Size: $(du -h "${DIST_DIR}/${DMG_NAME}" | cut -f1)"
    echo ""
    echo "Your DMG installer is ready for distribution!"
    echo "Users can double-click to mount and drag the app to Applications."
else
    echo "Error: Failed to create DMG" >&2
    exit 1
fi
