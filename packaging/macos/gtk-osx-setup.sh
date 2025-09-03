#!/usr/bin/env bash
set -euo pipefail

# Setup gtk-mac-bundler following PyGObject deployment guide
# References: https://pygobject.gnome.org/guide/deploy.html

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GTK_MAC_BUNDLER_DIR="${HOME}/gtk-mac-bundler"
GTK_MAC_BUNDLER_PREFIX=""
GTK_MAC_BUNDLER_INSTALL_DIR=""

echo "Setting up gtk-mac-bundler for sshPilot packaging"

if command -v brew >/dev/null 2>&1; then
  GTK_MAC_BUNDLER_PREFIX="$(brew --prefix)"
  # Ensure we have the GTK stack
  if ! brew list gtk4 >/dev/null 2>&1; then
    echo "Installing GTK4 stack..."
    brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection
  fi
elif command -v jhbuild >/dev/null 2>&1; then
  GTK_MAC_BUNDLER_PREFIX="$(jhbuild --prefix)"
else
  echo "Homebrew is required. Install it first:" >&2
  echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"" >&2
  exit 1
fi

# Install gtk-mac-bundler from source
if [ ! -d "${GTK_MAC_BUNDLER_DIR}" ]; then
  echo "Installing gtk-mac-bundler..."
  git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git "${GTK_MAC_BUNDLER_DIR}"
fi
cd "${GTK_MAC_BUNDLER_DIR}"
make install PREFIX="${GTK_MAC_BUNDLER_PREFIX}"
cd "${ROOT_DIR}"

# Directory where the executable was installed
GTK_MAC_BUNDLER_INSTALL_DIR="${GTK_MAC_BUNDLER_PREFIX}/bin"

# Add gtk-mac-bundler to PATH and verify
export PATH="${GTK_MAC_BUNDLER_INSTALL_DIR}:${PATH}"
if ! command -v gtk-mac-bundler >/dev/null 2>&1; then
  echo "gtk-mac-bundler installation failed" >&2
  exit 1
fi

echo "Setup complete! gtk-mac-bundler installed at ${GTK_MAC_BUNDLER_INSTALL_DIR}"

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "Now run: bash packaging/macos/make-bundle.sh"
fi

