#!/bin/bash

# Setup script for gtk-osx environment
# This script sets up jhbuild and gtk-osx for building sshPilot

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}Setting up gtk-osx environment for sshPilot...${NC}"

# Check if we're on macOS
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo -e "${RED}Error: This script is for macOS only${NC}"
    exit 1
fi

# Check for Homebrew
if ! command -v brew &> /dev/null; then
    echo -e "${RED}Error: Homebrew not found. Please install Homebrew first.${NC}"
    echo "Visit: https://brew.sh"
    exit 1
fi

# Install required Homebrew packages
echo -e "${GREEN}Installing required Homebrew packages...${NC}"
REQUIRED_PACKAGES=(
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
    "python3"
)

for package in "${REQUIRED_PACKAGES[@]}"; do
    if brew list "$package" &> /dev/null; then
        echo -e "${BLUE}✓ $package already installed${NC}"
    else
        echo -e "${YELLOW}Installing $package...${NC}"
        brew install "$package"
    fi
done

# Install jhbuild if not present
if ! command -v jhbuild &> /dev/null; then
    echo -e "${GREEN}Installing jhbuild...${NC}"
    # jhbuild uses autotools, install from source
    if [ ! -d "/tmp/jhbuild" ]; then
        git clone https://gitlab.gnome.org/GNOME/jhbuild.git /tmp/jhbuild
    fi
    cd /tmp/jhbuild
    ./autogen.sh --prefix=/usr/local
    make
    sudo make install
    echo -e "${GREEN}jhbuild installed successfully${NC}"
else
    echo -e "${BLUE}✓ jhbuild already installed${NC}"
fi

# Install gtk-mac-bundler manually (not available in Homebrew)
if ! command -v gtk-mac-bundler &> /dev/null; then
    echo -e "${GREEN}Installing gtk-mac-bundler...${NC}"
    if [ ! -d "/tmp/gtk-mac-bundler" ]; then
        git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git /tmp/gtk-mac-bundler
    fi
    cd /tmp/gtk-mac-bundler && make install
    echo -e "${GREEN}gtk-mac-bundler installed successfully${NC}"
else
    echo -e "${BLUE}✓ gtk-mac-bundler already installed${NC}"
fi

# Create gtk-osx directory structure
GTK_OSX_DIR="$HOME/gtk"
echo -e "${GREEN}Setting up gtk-osx directory structure...${NC}"

mkdir -p "$GTK_OSX_DIR/inst"
mkdir -p "$GTK_OSX_DIR/sources" 
mkdir -p "$GTK_OSX_DIR/build"

# Download gtk-osx moduleset if not present
MODULESET_DIR="$GTK_OSX_DIR/modulesets"
if [ ! -d "$MODULESET_DIR" ]; then
    echo -e "${GREEN}Downloading gtk-osx moduleset...${NC}"
    git clone https://gitlab.gnome.org/GNOME/gtk-osx.git "$GTK_OSX_DIR/gtk-osx"
    ln -sf "$GTK_OSX_DIR/gtk-osx/modulesets" "$MODULESET_DIR"
fi

# Create jhbuild configuration
echo -e "${GREEN}Creating jhbuild configuration...${NC}"
JHBUILD_CONFIG="$HOME/.jhbuildrc"
cp "$(dirname "${BASH_SOURCE[0]}")/jhbuildrc" "$JHBUILD_CONFIG"

# Set up environment variables
echo -e "${GREEN}Setting up environment variables...${NC}"
ENV_SETUP="$HOME/.gtk-osx-env"
cat > "$ENV_SETUP" << 'EOF'
#!/bin/bash
# gtk-osx environment setup

export PATH="$HOME/gtk/inst/bin:$PATH"
export PKG_CONFIG_PATH="$HOME/gtk/inst/lib/pkgconfig:/opt/homebrew/lib/pkgconfig:/usr/local/lib/pkgconfig"
export LD_LIBRARY_PATH="$HOME/gtk/inst/lib:/opt/homebrew/lib:/usr/local/lib"
export DYLD_LIBRARY_PATH="$HOME/gtk/inst/lib:/opt/homebrew/lib:/usr/local/lib"
export GI_TYPELIB_PATH="$HOME/gtk/inst/lib/girepository-1.0:/opt/homebrew/lib/girepository-1.0:/usr/local/lib/girepository-1.0"
export XDG_DATA_DIRS="$HOME/gtk/inst/share:/opt/homebrew/share:/usr/local/share"
export ACLOCAL_PATH="$HOME/gtk/inst/share/aclocal"
export MANPATH="$HOME/gtk/inst/share/man:$MANPATH"
EOF

chmod +x "$ENV_SETUP"

# Add to shell profile
SHELL_PROFILE=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_PROFILE="$HOME/.zshrc"
elif [ -f "$HOME/.bash_profile" ]; then
    SHELL_PROFILE="$HOME/.bash_profile"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_PROFILE="$HOME/.bashrc"
fi

if [ -n "$SHELL_PROFILE" ]; then
    if ! grep -q "gtk-osx-env" "$SHELL_PROFILE"; then
        echo -e "${GREEN}Adding gtk-osx environment to $SHELL_PROFILE...${NC}"
        echo "" >> "$SHELL_PROFILE"
        echo "# gtk-osx environment" >> "$SHELL_PROFILE"
        echo "source ~/.gtk-osx-env" >> "$SHELL_PROFILE"
    fi
fi

echo -e "${GREEN}gtk-osx environment setup completed!${NC}"
echo -e "${YELLOW}Next steps:${NC}"
echo "1. Restart your terminal or run: source ~/.gtk-osx-env"
echo "2. Run: jhbuild bootstrap"
echo "3. Run: jhbuild build meta-gtk-osx-bootstrap"
echo "4. Run: jhbuild build meta-gtk-osx-core"
echo "5. Run: jhbuild build meta-gtk-osx-python"
echo "6. Then you can build sshPilot with: ./build-bundle.sh"

echo -e "\n${BLUE}Note: The initial jhbuild process may take several hours to complete.${NC}"
echo -e "${BLUE}This is normal as it builds GTK+ and all dependencies from source.${NC}"
