#!/bin/bash
set -euo pipefail

# Build script that automatically handles the gdk-pixbuf hang issue
# This follows the official PyGObject deployment guide using gtk-mac-bundler

echo "Building sshPilot.app with gtk-mac-bundler..."

# Step 1: Replace gdk-pixbuf-query-loaders with dummy version
#echo "Replacing gdk-pixbuf-query-loaders to avoid hang..."
#sudo cp /opt/homebrew/bin/gdk-pixbuf-query-loaders.backup /opt/homebrew/bin/gdk-pixbuf-query-loaders 2>/dev/null || true
#sudo cp packaging/macos/dummy-gdk-pixbuf /opt/homebrew/bin/gdk-pixbuf-query-loaders

# Step 3: Build the bundle
echo "Building bundle..."
bash packaging/macos/make-bundle.sh

# Step 4: Clean up
echo "Cleaning up temporary files..."
rm -rf packaging/macos/temp-bin

echo "Build complete! The bundle is now in dist/sshPilot.app"
echo "You can now open: open dist/sshPilot.app"
