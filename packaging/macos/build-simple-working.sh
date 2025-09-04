#!/bin/bash

# Working simple build script for sshPilot macOS app bundle
# This version fixes the path issues

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

echo -e "${GREEN}Building sshPilot macOS app bundle (working version)...${NC}"

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

# Return to packaging directory for file operations
cd "$PACKAGING_DIR"

# Create the app bundle structure
echo -e "${GREEN}Creating app bundle structure...${NC}"
mkdir -p "$BUNDLE_PATH/Contents/MacOS"
mkdir -p "$BUNDLE_PATH/Contents/Resources"
mkdir -p "$BUNDLE_PATH/Contents/Resources/bin"

# Copy Info.plist and icon (using absolute paths)
echo -e "${GREEN}Copying bundle files...${NC}"
cp "Info.plist" "$BUNDLE_PATH/Contents/"
cp "sshpilot.icns" "$BUNDLE_PATH/Contents/Resources/"

# Copy resources
cp -r "$PROJECT_ROOT/sshpilot/resources" "$BUNDLE_PATH/Contents/Resources/"

# Copy the virtual environment to the bundle
echo -e "${GREEN}Copying virtual environment to bundle...${NC}"
cp -r "$BUILD_DIR/venv" "$BUNDLE_PATH/Contents/Resources/"

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
export GI_TYPELIB_PATH="/opt/homebrew/lib/girepository-1.0:/usr/local/lib/girepository-1.0:$GI_TYPELIB_PATH"
export PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig:/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH"
export LD_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:$LD_LIBRARY_PATH"
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:$DYLD_LIBRARY_PATH"
export XDG_DATA_DIRS="/opt/homebrew/share:/usr/local/share:$XDG_DATA_DIRS"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# Set working directory to resources
cd "$RESOURCES_DIR"

# Launch the application
exec "$RESOURCES_DIR/venv/bin/python3" "$RESOURCES_DIR/venv/run.py" "$@"
EOF

chmod +x "$BUNDLE_PATH/Contents/MacOS/sshPilot"

# Copy system binaries
echo -e "${GREEN}Copying system binaries...${NC}"
cp /opt/homebrew/bin/sshpass "$BUNDLE_PATH/Contents/Resources/bin/" 2>/dev/null || \
cp /usr/local/bin/sshpass "$BUNDLE_PATH/Contents/Resources/bin/" 2>/dev/null || \
echo -e "${YELLOW}Warning: sshpass not found in expected locations${NC}"

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
echo -e "${BLUE}Note: This bundle requires Homebrew packages to be installed on the target system.${NC}"
