#!/usr/bin/env bash
set -euo pipefail

# Setup gtk-mac-bundler following PyGObject deployment guide
# References: https://pygobject.gnome.org/guide/deploy.html

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GTK_MAC_BUNDLER_DIR="${HOME}/gtk-mac-bundler"
# Directory where the gtk-mac-bundler executable will be installed
GTK_MAC_BUNDLER_INSTALL_DIR=""

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
  make install
  cd "${ROOT_DIR}"
else
  # Ensure gtk-mac-bundler is installed even if repo exists
  cd "${GTK_MAC_BUNDLER_DIR}"
  make install
  cd "${ROOT_DIR}"
fi

# Determine where gtk-mac-bundler was installed
if command -v brew >/dev/null 2>&1; then
  GTK_MAC_BUNDLER_INSTALL_DIR="$(brew --prefix)/bin"
elif command -v jhbuild >/dev/null 2>&1; then
  GTK_MAC_BUNDLER_INSTALL_DIR="$(jhbuild --prefix)/bin"
else
  GTK_MAC_BUNDLER_INSTALL_DIR="${HOME}/.local/bin"
fi

# Add gtk-mac-bundler to PATH
export PATH="${GTK_MAC_BUNDLER_INSTALL_DIR}:${PATH}"

echo "Setup complete! gtk-mac-bundler installed at ${GTK_MAC_BUNDLER_INSTALL_DIR}"
echo "Now run: bash packaging/macos/make-bundle.sh"


