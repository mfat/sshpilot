#!/usr/bin/env bash
set -euo pipefail

MANIFEST=flatpak/io.github.mfat.sshpilot.test.yaml
BUILD_DIR=flatpak/build-sshpilot-test
REPO_DIR=flatpak/repo-test
REMOTE_NAME=sshpilot-test
APP_ID=io.github.mfat.sshpilot.test

if ! command -v flatpak-builder >/dev/null 2>&1; then
  echo "flatpak-builder not found. Install it (e.g., on Ubuntu: sudo apt install flatpak-builder)" >&2
  exit 1
fi
if ! command -v flatpak >/dev/null 2>&1; then
  echo "flatpak not found. Install it to proceed." >&2
  exit 1
fi

rm -rf "$BUILD_DIR" "$REPO_DIR"
mkdir -p "$BUILD_DIR"

echo "Building bundle from manifest: $MANIFEST"
flatpak-builder --force-clean --repo="$REPO_DIR" "$BUILD_DIR" "$MANIFEST"

# Add local repo
if flatpak remote-list | grep -q "^$REMOTE_NAME\b"; then
  echo "Remote $REMOTE_NAME already exists, removing..."
  flatpak remote-delete "$REMOTE_NAME"
fi
flatpak remote-add --no-gpg-verify --user "$REMOTE_NAME" "$REPO_DIR"

echo "Installing $APP_ID from $REMOTE_NAME"
flatpak install --user -y "$REMOTE_NAME" "$APP_ID"

echo "Done. You can run with: flatpak run $APP_ID"
