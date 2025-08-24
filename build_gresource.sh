#!/bin/bash

# Build script for GResource compilation
set -e

echo "Compiling GResource files..."

# Compile the main GResource bundle
cd sshpilot/resources
glib-compile-resources sshpilot.gresource.xml --target=sshpilot.gresource --sourcedir=.

echo "GResource compilation completed successfully!"
echo "Generated: sshpilot/resources/sshpilot.gresource"
