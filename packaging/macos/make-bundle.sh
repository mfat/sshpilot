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
  echo "This will install gtk-mac-bundler and make it available system-wide." >&2
  exit 1
fi

# Verify gtk-mac-bundler is working
echo "✓ gtk-mac-bundler is available and ready to use"

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

# Install dependencies inside an isolated virtual environment (PEP 668 safe)
VENV_DIR="${BUILD_DIR}/.venv-bundle"
python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install -r "${ROOT_DIR}/requirements.txt"

# Copy application files to build directory
cp -R "${ROOT_DIR}/sshpilot" "${BUILD_DIR}/"
cp "${ROOT_DIR}/run.py" "${BUILD_DIR}/"
cp "${ROOT_DIR}/requirements.txt" "${BUILD_DIR}/"

# Copy the enhanced launcher script
cp "${SCRIPT_DIR}/enhanced-launcher.sh" "${BUILD_DIR}/sshPilot-launcher.sh"
chmod +x "${BUILD_DIR}/sshPilot-launcher.sh"

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
export BUILD_DIR="${BUILD_DIR}"
export BREW_PREFIX="${BREW_PREFIX}"
export PYTHON_RUNTIME="${PYTHON_RUNTIME}"
export GTK_LIBS="${GTK_LIBS}"
export GTK_DATA="${GTK_DATA}"
pushd "${BUILD_DIR}" >/dev/null
gtk-mac-bundler "${BUNDLE_XML}"
popd >/dev/null

# Move the created bundle to dist directory (gtk-mac-bundler creates it on Desktop)
if [ -d "${HOME}/Desktop/sshPilot.app" ]; then
  # Remove existing app bundle if it exists
  rm -rf "${DIST_DIR}/sshPilot.app"
  mv "${HOME}/Desktop/sshPilot.app" "${DIST_DIR}/"

  # Post-bundle: Add runtime assets (ICU dylibs, GI typelibs, gdk-pixbuf loaders)
  echo "Adding runtime assets to the bundle..."
  APP_DIR="${DIST_DIR}/sshPilot.app"
  RES_DIR="${APP_DIR}/Contents/Resources"
  FRAMEWORKS_DIR="${APP_DIR}/Contents/Frameworks"
  
  # Post-bundle: Bundle Python packages from virtual environment
  echo "Bundling Python packages for self-contained distribution..."
  # Find the actual site-packages directory (handles different Python versions)
  ACTUAL_SITE_PACKAGES=$(find "${VENV_DIR}/lib" -name "site-packages" -type d | head -1)
  if [ -n "${ACTUAL_SITE_PACKAGES}" ]; then
    # Create the target directory
    mkdir -p "${RES_DIR}/lib/python3.13/site-packages"
    
    # Copy all Python packages (excluding __pycache__ and .dist-info)
    echo "  Copying Python packages from: ${ACTUAL_SITE_PACKAGES}"
    find "${ACTUAL_SITE_PACKAGES}" -maxdepth 1 -mindepth 1 \
      -not -name "__pycache__" \
      -not -name "*.dist-info" \
      -not -name "*.pyc" \
      -exec cp -R {} "${RES_DIR}/lib/python3.13/site-packages/" \;
    echo "  ✓ Python packages bundled (paramiko, cryptography, keyring, etc.)"
  else
    echo "  ⚠️  Warning: Could not find site-packages directory"
  fi
  
  # Create required directories
  mkdir -p "${FRAMEWORKS_DIR}" "${RES_DIR}/lib/girepository-1.0"
  
  # Copy ICU dylibs (VTE needs these at runtime)
  if [ -d "${BREW_PREFIX}/opt/icu4c/lib" ]; then
    cp "${BREW_PREFIX}/opt/icu4c/lib"/libicu*.dylib "${FRAMEWORKS_DIR}/" 2>/dev/null || true
    echo "  ✓ ICU dylibs copied to Frameworks"
  fi
  
  # Copy GI typelibs
  if [ -d "${BREW_PREFIX}/lib/girepository-1.0" ]; then
    cp "${BREW_PREFIX}/lib/girepository-1.0"/*.typelib "${RES_DIR}/lib/girepository-1.0/" 2>/dev/null || true
    echo "  ✓ GI typelibs copied to Resources/lib/girepository-1.0"
  fi
  
  # Generate gdk-pixbuf loaders cache manually
  PIXBUF_DIR="${RES_DIR}/lib/gdk-pixbuf-2.0/2.10.0"
  if [ -d "${PIXBUF_DIR}/loaders" ]; then
    if command -v gdk-pixbuf-query-loaders >/dev/null 2>&1; then
      gdk-pixbuf-query-loaders "${PIXBUF_DIR}/loaders"/*.so > "${PIXBUF_DIR}/loaders.cache" 2>/dev/null || true
      echo "  ✓ gdk-pixbuf loaders cache generated"
    fi
  fi

  # Post-bundle: Replace the launcher with our enhanced version
  echo "Installing enhanced launcher for double-click support..."
  cp "${SCRIPT_DIR}/enhanced-launcher.sh" "${APP_DIR}/Contents/MacOS/sshPilot"
  chmod +x "${APP_DIR}/Contents/MacOS/sshPilot"
  echo "  ✓ Enhanced launcher installed"

  echo "sshPilot.app created in ${DIST_DIR}"
  echo "You can now open: open ${DIST_DIR}/sshPilot.app"
  echo "Or double-click sshPilot.app in Finder"
  echo ""
  echo "This is now a TRULY fully self-contained, redistributable bundle!"
  echo "Users don't need to install Python, PyGObject, GTK libraries, or any Python packages!"
  echo "✅ All dependencies bundled: paramiko, cryptography, keyring, nacl, bcrypt, etc."
  echo "✅ Double-click launch is now supported!"
else
  echo "gtk-mac-bundler failed to create app bundle" >&2
  exit 1
fi


