#!/bin/bash
# Test Flatpak build: builds the in-tree manifest against the working tree.
#
# The manifest lives at the repo root (where GNOME Builder looks for it) and
# its `path: .` source is the working tree, so it builds in place.

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

flatpak run --command=flathub-build org.flatpak.Builder \
    --install \
    --disable-rofiles-fuse \
    --force-clean \
    io.github.mfat.sshpilot.yaml

echo ""
echo "=== Build complete ==="
