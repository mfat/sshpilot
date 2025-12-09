#!/bin/bash
# Test Flatpak build script
# Copies files from flatpak/ directory and runs flatpak build

set -e

echo "=== Test Flatpak Build ==="

# Copy all 3 files from flatpak/ directory to current directory
echo "Copying files from flatpak/ to current directory..."
cp flatpak/io.github.mfat.sshpilot.yaml .
cp flatpak/python3-deps.yml .
cp flatpak/sshpilot-launcher.sh .

echo "Files copied:"
ls -la io.github.mfat.sshpilot.yaml python3-deps.yml sshpilot-launcher.sh

# Run flatpak build
echo ""
echo "=== Running Flatpak build ==="
flatpak run --command=flathub-build org.flatpak.Builder --install --disable-rofiles-fuse --force-clean io.github.mfat.sshpilot.yaml

echo ""
echo "=== Build complete ==="

