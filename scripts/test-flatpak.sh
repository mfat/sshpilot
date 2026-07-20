#!/bin/bash
# Test Flatpak build script
# Copies files from flatpak/ directory and runs flatpak build

set -e

# Work from the repo root regardless of invocation directory
cd "$(dirname "$0")/.."

echo "=== Test Flatpak Build ==="

# The manifest resolves python3-deps.yml relative to itself, and its `type: dir`
# source is the repo root, so both must sit at the root to build.
echo "Copying files from flatpak/ to current directory..."
cp flatpak/io.github.mfat.sshpilot.yaml .
cp flatpak/python3-deps.yml .

echo "Files copied:"
ls -la io.github.mfat.sshpilot.yaml python3-deps.yml

# Run flatpak build
echo ""
echo "=== Running Flatpak build ==="
flatpak run --command=flathub-build org.flatpak.Builder --install --disable-rofiles-fuse --force-clean io.github.mfat.sshpilot.yaml

echo ""
echo "=== Build complete ==="

