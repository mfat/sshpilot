#!/bin/bash
# Install (sync) an example plugin into the user plugin dir so the running app
# picks it up. sshPilot loads plugins from ~/.local/share/sshpilot/plugins/,
# NOT from the repo — editing the source has no effect until you copy it there
# and restart the app.
#
# Usage:
#   scripts/install-plugin.sh                  # installs easyenv_workspaces (default)
#   scripts/install-plugin.sh mock_vps         # install a different example by dir name
#   scripts/install-plugin.sh /abs/path/to/plugin
#
# The destination dir name is the plugin id from plugin.json (falls back to the
# source dir name with underscores turned into hyphens).

set -eu

SRC_ARG="${1:-easyenv_workspaces}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLES_DIR="$REPO_ROOT/src/sshpilot/plugins/examples"
PLUGINS_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/sshpilot/plugins"

# Resolve the source directory.
if [ -d "$SRC_ARG" ]; then
    SRC="$(cd "$SRC_ARG" && pwd)"
elif [ -d "$EXAMPLES_DIR/$SRC_ARG" ]; then
    SRC="$EXAMPLES_DIR/$SRC_ARG"
else
    echo "Plugin source not found: '$SRC_ARG'" >&2
    echo "Looked in: $EXAMPLES_DIR/$SRC_ARG" >&2
    echo "Available examples:" >&2
    ls -1 "$EXAMPLES_DIR" 2>/dev/null | sed 's/^/  /' >&2
    exit 1
fi

if [ ! -f "$SRC/__init__.py" ]; then
    echo "No __init__.py in $SRC — not a plugin directory" >&2
    exit 1
fi

# Destination dir name = plugin id from plugin.json, else dirname with hyphens.
PLUGIN_ID=""
if [ -f "$SRC/plugin.json" ]; then
    PLUGIN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$SRC/plugin.json" 2>/dev/null || true)"
fi
if [ -z "$PLUGIN_ID" ]; then
    PLUGIN_ID="$(basename "$SRC" | tr '_' '-')"
fi

DST="$PLUGINS_BASE/$PLUGIN_ID"
mkdir -p "$DST"

# Copy the package contents (py + manifest + docs), preserving the stub/ removal:
# we mirror SRC -> DST and delete files in DST that no longer exist in SRC.
copied=0
for f in "$SRC"/*; do
    name="$(basename "$f")"
    [ -d "$f" ] && continue   # examples are flat; skip any nested dirs
    cp "$f" "$DST/$name"
    echo "  ✓ $name"
    copied=$((copied + 1))
done

echo ""
echo "Installed '$PLUGIN_ID' ($copied files) -> $DST"
echo "Restart sshPilot to load the updated plugin."
