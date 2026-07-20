#!/bin/bash
# Test Flatpak build: builds the in-tree manifest against the working tree.
#
# The manifest's `path: ..` source and its python3-deps.yml include are both
# resolved relative to the manifest itself, so it builds in place -- nothing
# needs copying to the repo root.

set -e

# Work from the repo root regardless of invocation directory
cd "$(dirname "$0")/.."

echo "=== Test Flatpak Build ==="
flatpak run --command=flathub-build org.flatpak.Builder --install --disable-rofiles-fuse --force-clean flatpak/io.github.mfat.sshpilot.yaml

echo ""
echo "=== Build complete ==="
