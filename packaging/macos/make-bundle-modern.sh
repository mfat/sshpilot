#!/bin/bash

# Modern macOS App Bundle Builder for sshPilot
# Based on build-guide.md - uses Python venv for truly self-contained bundles

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Build directories
BUILD_DIR="${ROOT_DIR}/build/macos-bundle"
DIST_DIR="${ROOT_DIR}/dist"

echo -e "${BLUE}🚀 Building sshPilot.app with modern venv approach...${NC}"

# Clean up previous builds
echo "Cleaning up previous builds..."
rm -rf "${BUILD_DIR}"
rm -rf "${DIST_DIR}/sshPilot.app"
mkdir -p "${BUILD_DIR}"
mkdir -p "${DIST_DIR}"

# Create the app bundle structure
echo "Creating app bundle structure..."
APP_BUNDLE="${BUILD_DIR}/sshPilot.app"
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources"

# Use modern venv approach: create Python environment directly inside the bundle
echo "Creating self-contained Python environment inside app bundle..."
PYTHON_VERSION="3.13"
if [ -f "/opt/homebrew/bin/python3" ]; then
    echo "Using Homebrew Python 3.13 for building..."
    /opt/homebrew/bin/python3 -m venv "${APP_BUNDLE}/Contents"
else
    echo "Using system Python for building..."
    python3 -m venv "${APP_BUNDLE}/Contents"
fi

# Install dependencies directly into the bundle's Python environment
echo "Installing dependencies into bundle's Python environment..."
"${APP_BUNDLE}/Contents/bin/pip" install --upgrade pip wheel setuptools
"${APP_BUNDLE}/Contents/bin/pip" install -r "${ROOT_DIR}/requirements.txt"

# Install the application itself
echo "Installing sshPilot application into bundle..."
"${APP_BUNDLE}/Contents/bin/pip" install "${ROOT_DIR}"

# Copy application files to bundle Resources
echo "Copying application files to bundle..."
cp -R "${ROOT_DIR}/sshpilot" "${APP_BUNDLE}/Contents/Resources/"
cp "${ROOT_DIR}/run.py" "${APP_BUNDLE}/Contents/Resources/"
cp "${ROOT_DIR}/requirements.txt" "${APP_BUNDLE}/Contents/Resources/"

# Create Info.plist for the app bundle
cat > "${APP_BUNDLE}/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>sshPilot</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.mfat.sshpilot</string>
    <key>CFBundleName</key>
    <string>sshPilot</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSRequiresAquaSystemAppearance</key>
    <false/>
    <key>CFBundleIconFile</key>
    <string>sshPilot</string>
</dict>
</plist>
EOF

# Create the launcher script (modern approach from build-guide.md)
cat > "${APP_BUNDLE}/Contents/MacOS/sshPilot" << 'EOF'
#!/bin/sh
SELF="$(cd "$(dirname "$0")"; pwd)"
CONTENTS="$SELF/.."
INSTALLDIR="$CONTENTS"

# Setup environment paths for GTK etc.
export DYLD_FALLBACK_LIBRARY_PATH="$CONTENTS/lib:$DYLD_FALLBACK_LIBRARY_PATH"
export GI_TYPELIB_PATH="$CONTENTS/lib/girepository-1.0:$GI_TYPELIB_PATH"
export XDG_DATA_DIRS="$CONTENTS/share:/usr/local/share:/usr/share"
export GSETTINGS_SCHEMA_DIR="$CONTENTS/share/glib-2.0/schemas"

exec "$INSTALLDIR/bin/python3" -c "from sshpilot.main import main; main()" "$@"
EOF

chmod +x "${APP_BUNDLE}/Contents/MacOS/sshPilot"

# Copy resources
cp "${ROOT_DIR}/sshpilot/resources/sshpilot.gresource" "${APP_BUNDLE}/Contents/Resources/"
cp "${ROOT_DIR}/sshpilot/resources/sshpilot.svg" "${APP_BUNDLE}/Contents/Resources/"

# Copy app icon
echo "  Copying app icon..."
if [ -f "${ROOT_DIR}/packaging/macos/sshPilot.icns" ]; then
    cp "${ROOT_DIR}/packaging/macos/sshPilot.icns" "${APP_BUNDLE}/Contents/Resources/"
    echo "    App icon copied successfully"
else
    echo "    Warning: App icon not found at ${ROOT_DIR}/packaging/macos/sshPilot.icns"
fi

# Copy sshpass binary
echo "  Copying sshpass binary..."
if [ -f "/opt/homebrew/bin/sshpass" ]; then
    cp "/opt/homebrew/bin/sshpass" "${APP_BUNDLE}/Contents/bin/"
    chmod +x "${APP_BUNDLE}/Contents/bin/sshpass"
    echo "    sshpass copied successfully"
else
    echo "    Warning: sshpass not found at /opt/homebrew/bin/sshpass"
fi

# Bundle native libraries (GTK, etc.) using the helper functions from build-guide.md
echo "Bundling native libraries..."

# Helper functions from build-guide.md
resolve_deps() {
  local lib=$1
  otool -L "$lib" | grep -e "^/opt/homebrew/" | while read dep _; do
    echo "$dep"
  done
}

fix_paths() {
  local lib=$1
  for dep in $(resolve_deps "$lib"); do
    install_name_tool -change "$dep" "@executable_path/../lib/$(basename "$dep")" "$lib"
  done
}

# Find and bundle native libraries
echo "  Finding native libraries..."
binlibs=$(find "${APP_BUNDLE}/Contents" -type f -name '*.so' -o -name '*.dylib')

for lib in $binlibs; do
  echo "  Processing: $lib"
  resolve_deps "$lib"
  fix_paths "$lib"
done | sort -u | while read lib; do
  if [ -f "$lib" ]; then
    echo "    Copying: $lib"
    cp "$lib" "${APP_BUNDLE}/Contents/lib/"
    chmod u+w "${APP_BUNDLE}/Contents/lib/$(basename "$lib")"
    fix_paths "${APP_BUNDLE}/Contents/lib/$(basename "$lib")"
  fi
done

# Copy GTK libraries and resources from Homebrew (resolve symlinks)
echo "  Copying GTK libraries from Homebrew (resolving symlinks)..."
if [ -d "/opt/homebrew/lib" ]; then
    mkdir -p "${APP_BUNDLE}/Contents/lib"
    
    # Copy dylib files using rsync to resolve symlinks
    echo "    Copying dylib files..."
    rsync -aL /opt/homebrew/lib/*.dylib "${APP_BUNDLE}/Contents/lib/" 2>/dev/null || true
    
    # Copy gdk-pixbuf-2.0 directory
    echo "    Copying gdk-pixbuf-2.0..."
    rsync -aL /opt/homebrew/lib/gdk-pixbuf-2.0/ "${APP_BUNDLE}/Contents/lib/gdk-pixbuf-2.0/" 2>/dev/null || true
    
    # Copy girepository-1.0 directory
    echo "    Copying girepository-1.0..."
    rsync -aL /opt/homebrew/lib/girepository-1.0/ "${APP_BUNDLE}/Contents/lib/girepository-1.0/" 2>/dev/null || true
    
    # Copy glib-2.0 share data
    echo "    Copying glib-2.0 share data..."
    rsync -aL /opt/homebrew/share/glib-2.0/ "${APP_BUNDLE}/Contents/share/glib-2.0/" 2>/dev/null || true
    
    # Copy icons properly (resolve symlinks to actual files)
    echo "  Copying icon files (resolving symlinks)..."
    mkdir -p "${APP_BUNDLE}/Contents/share/icons"
    if [ -d "/opt/homebrew/share/icons" ]; then
        # Copy Adwaita icons using rsync to resolve symlinks
        if [ -d "/opt/homebrew/share/icons/Adwaita" ]; then
            rsync -aL /opt/homebrew/share/icons/Adwaita/ "${APP_BUNDLE}/Contents/share/icons/Adwaita/"
        fi
        # Copy hicolor icons using rsync to resolve symlinks
        if [ -d "/opt/homebrew/share/icons/hicolor" ]; then
            rsync -aL /opt/homebrew/share/icons/hicolor/ "${APP_BUNDLE}/Contents/share/icons/hicolor/"
        fi
    fi
fi

# Fix library paths for bundled libraries
echo "  Fixing library paths for bundled libraries..."
find "${APP_BUNDLE}/Contents/lib" -name "*.dylib" -type f | while read lib; do
    echo "    Fixing paths for: $(basename "$lib")"
    # Fix paths to point to bundled libraries
    install_name_tool -change "/opt/homebrew/lib/libcairo.2.dylib" "@executable_path/../lib/libcairo.2.dylib" "$lib" 2>/dev/null || true
    install_name_tool -change "/opt/homebrew/lib/libpango-1.0.0.dylib" "@executable_path/../lib/libpango-1.0.0.dylib" "$lib" 2>/dev/null || true
    install_name_tool -change "/opt/homebrew/lib/libpangocairo-1.0.0.dylib" "@executable_path/../lib/libpangocairo-1.0.0.dylib" "$lib" 2>/dev/null || true
    install_name_tool -change "/opt/homebrew/lib/libgtk-4.1.dylib" "@executable_path/../lib/libgtk-4.1.dylib" "$lib" 2>/dev/null || true
    install_name_tool -change "/opt/homebrew/lib/libadwaita-1.dylib" "@executable_path/../lib/libadwaita-1.dylib" "$lib" 2>/dev/null || true
done

# Generate gdk-pixbuf loaders cache
echo "  Generating gdk-pixbuf loaders cache..."
if [ -d "${APP_BUNDLE}/Contents/lib/gdk-pixbuf-2.0" ]; then
    if [ -f "/opt/homebrew/bin/gdk-pixbuf-query-loaders" ]; then
        /opt/homebrew/bin/gdk-pixbuf-query-loaders > "${APP_BUNDLE}/Contents/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
    else
        echo "    Warning: gdk-pixbuf-query-loaders not found"
    fi
fi

# Sign the app bundle
echo "Signing app bundle..."
codesign --deep --force --sign - "${APP_BUNDLE}"

# Verify the signature
echo "Verifying app bundle signature..."
codesign --verify --verbose "${APP_BUNDLE}"

# Copy the final bundle to dist
echo "Copying final bundle to dist directory..."
cp -R "${APP_BUNDLE}" "${DIST_DIR}/"

echo ""
echo -e "${GREEN}✅ sshPilot.app created successfully using modern venv approach!${NC}"
echo -e "${BLUE}📍 Location: ${DIST_DIR}/sshPilot.app${NC}"
echo ""
echo -e "${YELLOW}🚀 You can now:${NC}"
echo "   • Double-click the app in Finder"
echo "   • Run: open ${DIST_DIR}/sshPilot.app"
echo "   • Distribute the app to other Macs"
echo ""
echo -e "${GREEN}🔒 The app is signed with an ad-hoc signature (no certificate required)${NC}"
echo -e "${GREEN}📦 The app is fully self-contained with all dependencies bundled${NC}"
echo -e "${GREEN}🐍 Uses modern Python venv approach for maximum compatibility${NC}"

# Clean up temporary files
echo "Cleaning up temporary files..."
rm -rf "${BUILD_DIR}"

echo "Build complete! The bundle is now in dist/sshPilot.app"
echo "You can now open: open dist/sshPilot.app"
