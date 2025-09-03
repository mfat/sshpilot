#!/bin/bash
set -euo pipefail

# Temporarily bypass gdk-pixbuf-query-loaders to avoid gtk-mac-bundler hang
# This tool hangs during the bundling process, so we'll generate the cache manually after

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "Bypassing gdk-pixbuf-query-loaders hang..."

# Find the tool
GDK_TOOL=""
for path in "/opt/homebrew/bin/gdk-pixbuf-query-loaders" "/usr/local/bin/gdk-pixbuf-query-loaders" "/usr/bin/gdk-pixbuf-query-loaders"; do
  if [ -x "$path" ]; then
    GDK_TOOL="$path"
    break
  fi
done

if [ -z "$GDK_TOOL" ]; then
  echo "gdk-pixbuf-query-loaders not found, proceeding with build..."
  bash "${SCRIPT_DIR}/make-bundle.sh"
  exit 0
fi

# Create a temporary shim that exits immediately
TMP_DIR="$(mktemp -d)"
SHIM_TOOL="${TMP_DIR}/gdk-pixbuf-query-loaders"

cat > "$SHIM_TOOL" <<'EOF'
#!/bin/bash
# Temporary shim to bypass gtk-mac-bundler hang
exit 0
EOF

chmod +x "$SHIM_TOOL"

# Temporarily replace the tool in PATH
export PATH="$TMP_DIR:$PATH"

echo "Using shim for gdk-pixbuf-query-loaders to avoid hang..."

# Run the bundler
bash "${SCRIPT_DIR}/make-bundle.sh"

# Clean up
rm -rf "$TMP_DIR"

echo "Build complete. gdk-pixbuf loaders cache will be generated manually in post-bundle step."
