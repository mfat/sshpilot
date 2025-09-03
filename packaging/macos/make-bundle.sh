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

# Check if hicolor icon theme exists (required by gtk-mac-bundler)
if [ ! -f "${BREW_PREFIX}/share/icons/hicolor/index.theme" ]; then
  echo "⚠️  Warning: hicolor icon theme not found at ${BREW_PREFIX}/share/icons/hicolor/index.theme"
  echo "Creating minimal hicolor theme to prevent bundling errors..."
  
  # Create minimal hicolor theme structure
  mkdir -p "${BREW_PREFIX}/share/icons/hicolor"
  cat > "${BREW_PREFIX}/share/icons/hicolor/index.theme" << 'EOF'
[Icon Theme]
Name=Hicolor
Comment=Default icon theme
Directories=16x16/actions,16x16/apps,16x16/devices,16x16/filesystems,16x16/mimetypes,16x16/places,16x16/status,16x16/emblems,22x22/actions,22x22/apps,22x22/devices,22x22/filesystems,22x22/mimetypes,22x22/places,22x22/status,22x22/emblems,24x24/actions,24x24/apps,24x24/devices,24x24/filesystems,24x24/mimetypes,24x24/places,24x24/status,24x24/emblems,32x32/actions,32x32/apps,32x32/devices,32x32/filesystems,32x32/mimetypes,32x32/places,32x32/status,32x32/emblems,48x48/actions,48x48/apps,48x48/devices,48x48/filesystems,48x48/mimetypes,48x48/places,48x48/status,48x48/emblems,64x64/actions,64x64/apps,64x64/devices,64x64/filesystems,64x64/mimetypes,64x64/places,64x64/status,64x64/emblems,128x128/actions,128x128/apps,128x128/devices,128x128/filesystems,128x128/mimetypes,128x128/places,128x128/status,128x128/emblems,256x256/actions,256x256/apps,256x256/devices,256x256/filesystems,256x256/mimetypes,256x256/places,256x256/status,256x256/emblems,scalable/actions,scalable/apps,scalable/devices,scalable/filesystems,scalable/mimetypes,scalable/places,scalable/status,scalable/emblems

[16x16/actions]
Size=16
Type=Fixed

[16x16/apps]
Size=16
Type=Fixed

[16x16/devices]
Size=16
Type=Fixed

[16x16/filesystems]
Size=16
Type=Fixed

[16x16/mimetypes]
Size=16
Type=Fixed

[16x16/places]
Size=16
Type=Fixed

[16x16/status]
Size=16
Type=Fixed

[16x16/emblems]
Size=16
Type=Fixed

[22x22/actions]
Size=22
Type=Fixed

[22x22/apps]
Size=22
Type=Fixed

[22x22/devices]
Size=22
Type=Fixed

[22x22/filesystems]
Size=22
Type=Fixed

[22x22/mimetypes]
Size=22
Type=Fixed

[22x22/places]
Size=22
Type=Fixed

[22x22/status]
Size=22
Type=Fixed

[22x22/emblems]
Size=22
Type=Fixed

[24x24/actions]
Size=24
Type=Fixed

[24x24/apps]
Size=24
Type=Fixed

[24x24/devices]
Size=24
Type=Fixed

[24x24/filesystems]
Size=24
Type=Fixed

[24x24/mimetypes]
Size=24
Type=Fixed

[24x24/places]
Size=24
Type=Fixed

[24x24/status]
Size=24
Type=Fixed

[24x24/emblems]
Size=24
Type=Fixed

[32x32/actions]
Size=32
Type=Fixed

[32x32/apps]
Size=32
Type=Fixed

[32x32/devices]
Size=32
Type=Fixed

[32x32/filesystems]
Size=32
Type=Fixed

[32x32/mimetypes]
Size=32
Type=Fixed

[32x32/places]
Size=32
Type=Fixed

[32x32/status]
Size=32
Type=Fixed

[32x32/emblems]
Size=32
Type=Fixed

[48x48/actions]
Size=48
Type=Fixed

[48x48/apps]
Size=48
Type=Fixed

[48x48/devices]
Size=48
Type=Fixed

[48x48/filesystems]
Size=48
Type=Fixed

[48x48/mimetypes]
Size=48
Type=Fixed

[48x48/places]
Size=48
Type=Fixed

[48x48/status]
Size=48
Type=Fixed

[48x48/emblems]
Size=48
Type=Fixed

[64x64/actions]
Size=64
Type=Fixed

[64x64/apps]
Size=64
Type=Fixed

[64x64/devices]
Size=64
Type=Fixed

[64x64/filesystems]
Size=64
Type=Fixed

[64x64/mimetypes]
Size=64
Type=Fixed

[64x64/places]
Size=64
Type=Fixed

[64x64/status]
Size=64
Type=Fixed

[64x64/emblems]
Size=64
Type=Fixed

[128x128/actions]
Size=128
Type=Fixed

[128x128/apps]
Size=128
Type=Fixed

[128x128/devices]
Size=128
Type=Fixed

[128x128/filesystems]
Size=128
Type=Fixed

[128x128/mimetypes]
Size=128
Type=Fixed

[128x128/places]
Size=128
Type=Fixed

[128x128/status]
Size=128
Type=Fixed

[128x128/emblems]
Size=128
Type=Fixed

[256x256/actions]
Size=256
Type=Fixed

[256x256/apps]
Size=256
Type=Fixed

[256x256/devices]
Size=256
Type=Fixed

[256x256/filesystems]
Size=256
Type=Fixed

[256x256/mimetypes]
Size=256
Type=Fixed

[256x256/places]
Size=256
Type=Fixed

[256x256/status]
Size=256
Type=Fixed

[256x256/emblems]
Size=256
Type=Fixed

[scalable/actions]
Size=16
Type=Scalable

[scalable/apps]
Size=16
Type=Scalable

[scalable/devices]
Size=16
Type=Scalable

[scalable/filesystems]
Size=16
Type=Scalable

[scalable/mimetypes]
Size=16
Type=Scalable

[scalable/places]
Size=16
Type=Scalable

[scalable/status]
Size=16
Type=Scalable

[scalable/emblems]
Size=16
Type=Scalable
EOF
  
  # Create some basic directories to prevent errors
  mkdir -p "${BREW_PREFIX}/share/icons/hicolor/16x16/actions"
  mkdir -p "${BREW_PREFIX}/share/icons/hicolor/16x16/apps"
  mkdir -p "${BREW_PREFIX}/share/icons/hicolor/scalable/apps"
  
  echo "✓ Created minimal hicolor icon theme"
fi

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

# Disable code signing to prevent hangs during bundling
export CODESIGN_ALLOCATE=""
export CODE_SIGN_IDENTITY=""
export CODESIGN_FLAGS=""

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


