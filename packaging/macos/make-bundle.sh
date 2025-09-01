#!/usr/bin/env bash
set -euo pipefail

# Build sshPilot.app using gtk-mac-bundler following PyGObject deployment guide.
# Docs: https://pygobject.gnome.org/guide/deploy.html

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUNDLE_XML="${SCRIPT_DIR}/io.github.mfat.sshpilot.bundle"
DIST_DIR="${ROOT_DIR}/dist"
BUILD_DIR="${ROOT_DIR}/build/macos-bundle"

mkdir -p "${DIST_DIR}"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

# Check if gtk-mac-bundler is available
if ! command -v gtk-mac-bundler >/dev/null 2>&1; then
  echo "gtk-mac-bundler not found. Run gtk-osx-setup.sh first." >&2
  exit 1
fi

# Check if Homebrew GTK stack is available
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install it first." >&2
  exit 1
fi

BREW_PREFIX="$(brew --prefix)"
export PATH="${BREW_PREFIX}/bin:${PATH}"
export GI_TYPELIB_PATH="${BREW_PREFIX}/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="${BREW_PREFIX}/lib:${DYLD_LIBRARY_PATH:-}"

# Precompile resources if needed
if [ -f "${ROOT_DIR}/sshpilot/resources/sshpilot.gresource.xml" ]; then
  glib-compile-resources --sourcedir="${ROOT_DIR}/sshpilot/resources" \
    --target="${ROOT_DIR}/sshpilot/resources/sshpilot.gresource" \
    "${ROOT_DIR}/sshpilot/resources/sshpilot.gresource.xml"
fi

# Build the Python application (no PyInstaller, just Python)
echo "Building Python application..."

# Check if we're in a CI environment or if dependencies are already installed
if [ -z "${CI:-}" ] && [ -z "${GITHUB_ACTIONS:-}" ]; then
  # Only install dependencies locally, not in CI
  echo "Installing Python dependencies locally..."
  python3 -m pip install --user --upgrade pip
  python3 -m pip install --user -r "${ROOT_DIR}/requirements.txt"
else
  echo "Running in CI environment, skipping pip install (dependencies should be pre-installed)"
fi

# Copy application files to build directory
cp -R "${ROOT_DIR}/sshpilot" "${BUILD_DIR}/"
cp "${ROOT_DIR}/run.py" "${BUILD_DIR}/"
cp "${ROOT_DIR}/requirements.txt" "${BUILD_DIR}/"

# Copy the Python launcher
cp "${SCRIPT_DIR}/sshPilot-launcher-main.py" "${BUILD_DIR}/"
chmod +x "${BUILD_DIR}/sshPilot-launcher-main.py"

# Copy resources
cp "${ROOT_DIR}/sshpilot/resources/sshpilot.gresource" "${BUILD_DIR}/"
cp "${ROOT_DIR}/sshpilot/resources/sshpilot.svg" "${BUILD_DIR}/"

# Copy the Info.plist file
cp "${SCRIPT_DIR}/Info.plist" "${BUILD_DIR}/"

# Copy the app icon
cp "${SCRIPT_DIR}/sshPilot.icns" "${BUILD_DIR}/"

# Set up environment variables for gtk-mac-bundler
export BUILD_DIR="${BUILD_DIR}"
export BREW_PREFIX="${BREW_PREFIX}"

# Set up Python runtime and GTK paths for bundling
export PYTHON_RUNTIME="${BREW_PREFIX}/bin/python3"
export GTK_LIBS="${BREW_PREFIX}/lib"
export GTK_DATA="${BREW_PREFIX}/share"

# Use gtk-mac-bundler to create the app bundle
echo "Creating app bundle with gtk-mac-bundler..."

# Change to build directory and run gtk-mac-bundler
pushd "${BUILD_DIR}" >/dev/null
gtk-mac-bundler "${BUNDLE_XML}"
popd >/dev/null

# Move the created bundle to dist directory (gtk-mac-bundler creates it on Desktop)
if [ -d "${HOME}/Desktop/sshPilot.app" ]; then
  # Remove existing app bundle if it exists
  rm -rf "${DIST_DIR}/sshPilot.app"
  mv "${HOME}/Desktop/sshPilot.app" "${DIST_DIR}/"

  # Sign the app bundle for macOS compatibility (allows double-click)
  echo "Signing app bundle..."
  codesign --force --deep --sign - "${DIST_DIR}/sshPilot.app"

  echo "sshPilot.app created in ${DIST_DIR}"
  echo "You can now open: open ${DIST_DIR}/sshPilot.app"
  echo "Or double-click sshPilot.app in Finder"
  echo ""
  echo "This is now a fully self-contained, redistributable bundle!"
  echo "Users don't need to install Python, PyGObject, or GTK libraries!"
else
  echo "gtk-mac-bundler failed to create app bundle" >&2
  exit 1
fi


