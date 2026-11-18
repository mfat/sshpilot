#!/bin/bash
# Test Flatpak build: builds the working tree with the Flathub manifest.
#
# flatpak/io.github.mfat.sshpilot.yaml is the copy of the Flathub manifest;
# scripts/flatpak-dev-manifest.py derives the root manifest from it (sshPilot
# built from this checkout), at the repo root where GNOME Builder looks for it.

set -e

# Work from the repo root regardless of invocation directory
cd "$(dirname "$0")/.."

echo "=== Test Flatpak Build ==="

# Older runs of this script used repo as the build directory.  Remove that
# generated, non-OSTree directory once so the corrected invocation can create
# a proper repository in its place.
if [ -e repo ] && [ ! -d repo/objects ]; then
    rm -rf repo
fi

python3 scripts/flatpak-dev-manifest.py

flatpak run --command=flathub-build org.flatpak.Builder \
    --install \
    --disable-rofiles-fuse \
    --force-clean \
    io.github.mfat.sshpilot.yaml

echo ""
echo "=== Build complete ==="
