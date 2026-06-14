#!/bin/bash

# Build script for GResource compilation
set -e

# Work from the repo root regardless of invocation directory
cd "$(dirname "$0")/.."

echo "Compiling GResource files..."

cd sshpilot/resources
glib-compile-resources sshpilot.gresource.xml --target=sshpilot.gresource --sourcedir=.

echo "GResource compilation completed successfully!"
echo "Generated: sshpilot/resources/sshpilot.gresource"
