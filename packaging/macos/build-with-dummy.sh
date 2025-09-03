#!/bin/bash
set -euo pipefail

# Build script that temporarily replaces gdk-pixbuf-query-loaders to avoid hang
# This follows the official PyGObject deployment guide using gtk-mac-bundler

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "Building sshPilot.app with gtk-mac-bundler..."

# Find the real gdk-pixbuf-query-loaders
REAL_TOOL=""
for path in "/opt/homebrew/bin/gdk-pixbuf-query-loaders" "/usr/local/bin/gdk-pixbuf-query-loaders" "/usr/bin/gdk-pixbuf-query-loaders"; do
  if [ -x "$path" ]; then
    REAL_TOOL="$path"
    break
  fi
done

if [ -z "$REAL_TOOL" ]; then
  echo "gdk-pixbuf-query-loaders not found, proceeding with build..."
  bash "${SCRIPT_DIR}/make-bundle.sh"
  exit 0
fi

echo "Found gdk-pixbuf-query-loaders at: $REAL_TOOL"
echo "Temporarily replacing with dummy version to avoid hang..."

# Create temporary directory and replace the tool
TMP_DIR="$(mktemp -d)"
DUMMY_TOOL="${TMP_DIR}/gdk-pixbuf-query-loaders"
cp "${SCRIPT_DIR}/dummy-gdk-pixbuf" "${DUMMY_TOOL}"
chmod +x "${DUMMY_TOOL}"

# Temporarily replace the tool in PATH
export PATH="${TMP_DIR}:$PATH"

echo "Building bundle with dummy gdk-pixbuf-query-loaders..."
bash "${SCRIPT_DIR}/make-bundle.sh"

# Clean up
rm -rf "${TMP_DIR}"

echo "Build complete! The bundle is now in dist/sshPilot.app"
echo "gdk-pixbuf loaders cache will be generated manually in the post-bundle step."
