#!/bin/bash

# Standalone build script for sshPilot macOS app bundle
# Creates a truly self-contained bundle with all dependencies
# This script copies all necessary libraries from Homebrew

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGING_DIR="$(dirname "${BASH_SOURCE[0]}")"
BUILD_DIR="$PACKAGING_DIR/build"
BUNDLE_NAME="sshPilot.app"
BUNDLE_PATH="$BUILD_DIR/$BUNDLE_NAME"

# Homebrew paths
HOMEBREW_PREFIX="/opt/homebrew"
if [ ! -d "$HOMEBREW_PREFIX" ]; then
    HOMEBREW_PREFIX="/usr/local"
fi

echo -e "${GREEN}Building standalone sshPilot macOS app bundle...${NC}"

# Clean previous build
if [ -d "$BUILD_DIR" ]; then
    echo -e "${YELLOW}Cleaning previous build...${NC}"
    rm -rf "$BUILD_DIR"
fi

mkdir -p "$BUILD_DIR"

# Check for required tools
echo -e "${GREEN}Checking dependencies...${NC}"

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

# Create the app bundle structure
echo -e "${GREEN}Creating app bundle structure...${NC}"
mkdir -p "$BUNDLE_PATH/Contents/MacOS"
mkdir -p "$BUNDLE_PATH/Contents/Resources"
mkdir -p "$BUNDLE_PATH/Contents/Resources/lib"
mkdir -p "$BUNDLE_PATH/Contents/Resources/bin"
mkdir -p "$BUNDLE_PATH/Contents/Resources/share"
mkdir -p "$BUNDLE_PATH/Contents/Resources/lib/girepository-1.0"

# Copy Info.plist
cp "$PACKAGING_DIR/Info.plist" "$BUNDLE_PATH/Contents/"

# Copy icon
cp "$PACKAGING_DIR/sshpilot.icns" "$BUNDLE_PATH/Contents/Resources/"

# Copy resources
cp -r "$PROJECT_ROOT/sshpilot/resources" "$BUNDLE_PATH/Contents/Resources/"

# Copy the virtual environment to the bundle
echo -e "${GREEN}Copying virtual environment to bundle...${NC}"
cp -r "$BUILD_DIR/venv" "$BUNDLE_PATH/Contents/Resources/"

# Copy Homebrew libraries
echo -e "${GREEN}Copying Homebrew libraries...${NC}"

# Libraries to copy
LIBRARIES=(
    "libgtk-4.1.dylib"
    "libgdk-4.1.dylib"
    "libgraphene-1.0.dylib"
    "libadwaita-1.dylib"
    "libglib-2.0.dylib"
    "libgobject-2.0.dylib"
    "libgmodule-2.0.dylib"
    "libgthread-2.0.dylib"
    "libgio-2.0.dylib"
    "libcairo.dylib"
    "libcairo-gobject.dylib"
    "libpango-1.0.dylib"
    "libpangocairo-1.0.dylib"
    "libpangoft2-1.0.dylib"
    "libharfbuzz.dylib"
    "libfribidi.dylib"
    "libgdk_pixbuf-2.0.dylib"
    "libvte-3.91.dylib"
    "libgirepository-1.0.dylib"
    "libgjs.dylib"
    "libffi.dylib"
    "libintl.dylib"
    "libicudata.dylib"
    "libicui18n.dylib"
    "libicuuc.dylib"
    "libssl.dylib"
    "libcrypto.dylib"
    "libz.dylib"
    "libbz2.dylib"
    "liblzma.dylib"
    "libxml2.dylib"
    "libxslt.dylib"
    "libsqlite3.dylib"
    "libreadline.dylib"
    "libncurses.dylib"
    "libtinfo.dylib"
    "libedit.dylib"
    "libform.dylib"
    "libmenu.dylib"
    "libpanel.dylib"
)

# Copy libraries
for lib in "${LIBRARIES[@]}"; do
    if [ -f "$HOMEBREW_PREFIX/lib/$lib" ]; then
        cp "$HOMEBREW_PREFIX/lib/$lib" "$BUNDLE_PATH/Contents/Resources/lib/"
        echo -e "${BLUE}Copied: $lib${NC}"
    fi
done

# Copy GI typelibs
echo -e "${GREEN}Copying GI typelibs...${NC}"
GI_TYPELIBS=(
    "Gtk-4.0.typelib"
    "Adw-1.typelib"
    "Vte-3.91.typelib"
    "Gio-2.0.typelib"
    "GLib-2.0.typelib"
    "GObject-2.0.typelib"
    "GModule-2.0.typelib"
    "GThread-2.0.typelib"
    "cairo-1.0.typelib"
    "Pango-1.0.typelib"
    "PangoCairo-1.0.typelib"
    "PangoFT2-1.0.typelib"
    "HarfBuzz-0.0.typelib"
    "GdkPixbuf-2.0.typelib"
    "GIRepository-2.0.typelib"
    "Gjs-1.0.typelib"
    "FFI-1.0.typelib"
    "ICU-1.0.typelib"
)

for typelib in "${GI_TYPELIBS[@]}"; do
    if [ -f "$HOMEBREW_PREFIX/lib/girepository-1.0/$typelib" ]; then
        cp "$HOMEBREW_PREFIX/lib/girepository-1.0/$typelib" "$BUNDLE_PATH/Contents/Resources/lib/girepository-1.0/"
        echo -e "${BLUE}Copied: $typelib${NC}"
    fi
done

# Copy system binaries
echo -e "${GREEN}Copying system binaries...${NC}"
cp "$HOMEBREW_PREFIX/bin/sshpass" "$BUNDLE_PATH/Contents/Resources/bin/" 2>/dev/null || \
echo -e "${YELLOW}Warning: sshpass not found${NC}"

# Copy Python executable
cp "$HOMEBREW_PREFIX/bin/python3" "$BUNDLE_PATH/Contents/Resources/bin/" 2>/dev/null || \
cp /usr/bin/python3 "$BUNDLE_PATH/Contents/Resources/bin/" 2>/dev/null || \
echo -e "${YELLOW}Warning: python3 not found${NC}"

# Copy share data
echo -e "${GREEN}Copying share data...${NC}"
if [ -d "$HOMEBREW_PREFIX/share/adwaita-icon-theme" ]; then
    cp -r "$HOMEBREW_PREFIX/share/adwaita-icon-theme" "$BUNDLE_PATH/Contents/Resources/share/"
fi

if [ -d "$HOMEBREW_PREFIX/share/glib-2.0" ]; then
    cp -r "$HOMEBREW_PREFIX/share/glib-2.0" "$BUNDLE_PATH/Contents/Resources/share/"
fi

if [ -d "$HOMEBREW_PREFIX/share/gtk-4.0" ]; then
    cp -r "$HOMEBREW_PREFIX/share/gtk-4.0" "$BUNDLE_PATH/Contents/Resources/share/"
fi

# Create the main executable
echo -e "${GREEN}Creating main executable...${NC}"
cat > "$BUNDLE_PATH/Contents/MacOS/sshPilot" << 'EOF'
#!/bin/bash

# sshPilot main executable
# Sets up environment and launches the application

# Get the bundle directory
BUNDLE_DIR="$(dirname "$(dirname "$(dirname "$(dirname "${BASH_SOURCE[0]}")")")")"
RESOURCES_DIR="$BUNDLE_DIR/Contents/Resources"

# Set up environment variables for standalone bundle
export PYTHONPATH="$RESOURCES_DIR/venv/lib/python*/site-packages:$PYTHONPATH"
export GI_TYPELIB_PATH="$RESOURCES_DIR/lib/girepository-1.0:$GI_TYPELIB_PATH"
export PKG_CONFIG_PATH="$RESOURCES_DIR/lib/pkgconfig:$PKG_CONFIG_PATH"
export LD_LIBRARY_PATH="$RESOURCES_DIR/lib:$LD_LIBRARY_PATH"
export DYLD_LIBRARY_PATH="$RESOURCES_DIR/lib:$DYLD_LIBRARY_PATH"
export XDG_DATA_DIRS="$RESOURCES_DIR/share:$XDG_DATA_DIRS"
export PATH="$RESOURCES_DIR/bin:$PATH"

# Set working directory to resources
cd "$RESOURCES_DIR"

# Launch the application
exec "$RESOURCES_DIR/bin/python3" "$RESOURCES_DIR/venv/run.py" "$@"
EOF

chmod +x "$BUNDLE_PATH/Contents/MacOS/sshPilot"

# Fix library paths in the bundle
echo -e "${GREEN}Fixing library paths...${NC}"
BUNDLE_LIB_DIR="$BUNDLE_PATH/Contents/Resources/lib"

# Function to fix library paths
fix_library_paths() {
    local file="$1"
    local bundle_lib_dir="$2"
    
    # Get list of libraries this file depends on
    local deps=$(otool -L "$file" 2>/dev/null | grep -E "^\s+$HOMEBREW_PREFIX" | awk '{print $1}' || true)
    
    for dep in $deps; do
        local lib_name=$(basename "$dep")
        if [ -f "$bundle_lib_dir/$lib_name" ]; then
            # Change the library path to use the bundled version
            install_name_tool -change "$dep" "@executable_path/../Resources/lib/$lib_name" "$file" 2>/dev/null || true
        fi
    done
}

# Fix paths for all libraries in the bundle
for lib in "$BUNDLE_LIB_DIR"/*.dylib; do
    if [ -f "$lib" ]; then
        fix_library_paths "$lib" "$BUNDLE_LIB_DIR"
    fi
done

# Fix paths for the main executable
fix_library_paths "$BUNDLE_PATH/Contents/MacOS/sshPilot" "$BUNDLE_LIB_DIR"

# Fix paths for Python executable
if [ -f "$BUNDLE_PATH/Contents/Resources/bin/python3" ]; then
    fix_library_paths "$BUNDLE_PATH/Contents/Resources/bin/python3" "$BUNDLE_LIB_DIR"
fi

# Verify bundle was created
if [ ! -d "$BUNDLE_PATH" ]; then
    echo -e "${RED}Error: App bundle was not created successfully${NC}"
    exit 1
fi

echo -e "${GREEN}Standalone app bundle created successfully at: $BUNDLE_PATH${NC}"

# Run tests
echo -e "${GREEN}Running bundle tests...${NC}"
"$PACKAGING_DIR/test-bundle.sh" "$BUNDLE_PATH"

echo -e "${GREEN}Build completed successfully!${NC}"
echo -e "${YELLOW}App bundle location: $BUNDLE_PATH${NC}"
echo -e "${BLUE}This is a standalone bundle that should work on systems without Homebrew.${NC}"
