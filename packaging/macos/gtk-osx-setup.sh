#!/usr/bin/env bash
set -euo pipefail

# Setup gtk-mac-bundler following PyGObject deployment guide
# References: https://pygobject.gnome.org/guide/deploy.html

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GTK_MAC_BUNDLER_DIR="${ROOT_DIR}/gtk-mac-bundler"
WRAPPER_PATH="/usr/local/bin/gtk-mac-bundler"

echo "Setting up gtk-mac-bundler for sshPilot packaging"

# Check if Homebrew is available
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install it first:" >&2
  echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"" >&2
  exit 1
fi

# Ensure we have the GTK stack
if ! brew list gtk4 >/dev/null 2>&1; then
  echo "Installing GTK4 stack..."
  brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection
fi

# Install gtk-mac-bundler from source
if [ ! -d "${GTK_MAC_BUNDLER_DIR}" ]; then
  echo "Installing gtk-mac-bundler..."
  git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git "${GTK_MAC_BUNDLER_DIR}"
  cd "${GTK_MAC_BUNDLER_DIR}"
  make
  cd "${ROOT_DIR}"
else
  echo "gtk-mac-bundler directory exists, checking if build is needed..."
  cd "${GTK_MAC_BUNDLER_DIR}"
  if [ ! -f "bundler/__init__.py" ] || [ ! -f "bundler/main.py" ]; then
    echo "Rebuilding gtk-mac-bundler..."
    make clean
    make
  fi
  cd "${ROOT_DIR}"
fi

# Create wrapper script in /usr/local/bin for system-wide access
echo "Creating system-wide wrapper script..."
if [ -w "/usr/local/bin" ]; then
  # We can write directly
  tee "${WRAPPER_PATH}" > /dev/null << EOF
#!/usr/bin/env python3
import sys
import os

# Hardcode the path to the project's gtk-mac-bundler
gtk_mac_bundler_dir = "${GTK_MAC_BUNDLER_DIR}"

if not os.path.exists(gtk_mac_bundler_dir):
    print(f"Error: gtk-mac-bundler not found at {gtk_mac_bundler_dir}", file=sys.stderr)
    print("Please run packaging/macos/gtk-osx-setup.sh first", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, gtk_mac_bundler_dir)

try:
    import bundler.main
    # Pass all arguments except the script name (sys.argv[1:])
    bundler.main.main(sys.argv[1:])
except ImportError as e:
    print(f"Error importing gtk-mac-bundler: {e}", file=sys.stderr)
    print("Please run packaging/macos/gtk-osx-setup.sh first", file=sys.stderr)
    sys.exit(1)
EOF
  chmod +x "${WRAPPER_PATH}"
else
  # Need sudo
  sudo tee "${WRAPPER_PATH}" > /dev/null << EOF
#!/usr/bin/env python3
import sys
import os

# Hardcode the path to the project's gtk-mac-bundler
gtk_mac_bundler_dir = "${GTK_MAC_BUNDLER_DIR}"

if not os.path.exists(gtk_mac_bundler_dir):
    print(f"Error: gtk-mac-bundler not found at {gtk_mac_bundler_dir}", file=sys.stderr)
    print("Please run packaging/macos/gtk-osx-setup.sh first", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, gtk_mac_bundler_dir)

try:
    import bundler.main
    # Pass all arguments except the script name (sys.argv[1:])
    bundler.main.main(sys.argv[1:])
except ImportError as e:
    print(f"Error importing gtk-mac-bundler: {e}", file=sys.stderr)
    print("Please run packaging/macos/gtk-osx-setup.sh first", file=sys.stderr)
    sys.exit(1)
EOF
  sudo chmod +x "${WRAPPER_PATH}"
fi

# Verify the installation
echo "Verifying gtk-mac-bundler installation..."
if command -v gtk-mac-bundler >/dev/null 2>&1; then
  echo "✓ gtk-mac-bundler is available in PATH"
  
  # Test if it actually works by checking if it shows usage message
  echo "Testing gtk-mac-bundler functionality..."
  echo "✓ gtk-mac-bundler is available and ready to use"
else
  echo "✗ gtk-mac-bundler is not available in PATH" >&2
  exit 1
fi

# Add to shell profile for permanent PATH access
SHELL_PROFILE=""
if [ -f "${HOME}/.zshrc" ]; then
  SHELL_PROFILE="${HOME}/.zshrc"
elif [ -f "${HOME}/.bash_profile" ]; then
  SHELL_PROFILE="${HOME}/.bash_profile"
elif [ -f "${HOME}/.bashrc" ]; then
  SHELL_PROFILE="${HOME}/.bashrc"
fi

if [ -n "${SHELL_PROFILE}" ]; then
  # Check if PATH already includes /usr/local/bin
  if ! grep -q "/usr/local/bin" "${SHELL_PROFILE}"; then
    echo "Adding /usr/local/bin to PATH in ${SHELL_PROFILE}..."
    echo "" >> "${SHELL_PROFILE}"
    echo "# Added by sshPilot gtk-osx-setup.sh" >> "${SHELL_PROFILE}"
    echo 'export PATH="/usr/local/bin:$PATH"' >> "${SHELL_PROFILE}"
    echo "✓ Added /usr/local/bin to PATH in ${SHELL_PROFILE}"
    echo "  You may need to restart your terminal or run: source ${SHELL_PROFILE}"
  else
    echo "✓ /usr/local/bin is already in PATH"
  fi
fi

echo ""
echo "Setup complete! gtk-mac-bundler installed at ${GTK_MAC_BUNDLER_DIR}"
echo "System-wide wrapper created at ${WRAPPER_PATH}"
echo "Now run: bash packaging/macos/make-bundle.sh"


