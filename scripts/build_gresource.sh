#!/bin/bash

# Build script for GResource compilation
set -e

# Work from the repo root regardless of invocation directory
cd "$(dirname "$0")/.."

# Compile Blueprint .blp -> .ui, if any .blp exist. The generated .ui files are
# committed (like the .gresource binary), so packagers without blueprint-compiler
# just use them as-is; this step only re-runs for developers editing a .blp.
if ls sshpilot/resources/ui/*.blp >/dev/null 2>&1; then
  if command -v blueprint-compiler >/dev/null 2>&1; then
    echo "Compiling Blueprint .blp -> .ui..."
    blueprint-compiler batch-compile \
      sshpilot/resources/ui \
      sshpilot/resources/ui \
      sshpilot/resources/ui/*.blp
  else
    echo "WARNING: blueprint-compiler not found; using committed .ui files as-is." >&2
  fi
fi

echo "Compiling GResource files..."

cd sshpilot/resources
glib-compile-resources sshpilot.gresource.xml --target=sshpilot.gresource --sourcedir=.

echo "GResource compilation completed successfully!"
echo "Generated: sshpilot/resources/sshpilot.gresource"
