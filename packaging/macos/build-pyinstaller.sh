#!/usr/bin/env bash
# Build a fully self-contained sshPilot.app using PyInstaller on macOS.
# This script bundles Python, GTK, libadwaita and all Homebrew libraries
# so the resulting .app can be redistributed without external dependencies.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SPEC_FILE="${SCRIPT_DIR}/sshPilot-pyinstaller.spec"
DIST_DIR="${ROOT_DIR}/dist"
APP_BUNDLE="${DIST_DIR}/sshPilot.app"
FRAMEWORKS_DIR="${APP_BUNDLE}/Contents/Frameworks"
RES_DIR="${APP_BUNDLE}/Contents/Resources"

# Ensure required tools are available
command -v pyinstaller >/dev/null 2>&1 || { echo "pyinstaller not found; run 'pip install pyinstaller'." >&2; exit 1; }
command -v brew >/dev/null 2>&1 || { echo "Homebrew not found; install from https://brew.sh" >&2; exit 1; }
BREW_PREFIX="${BREW_PREFIX:-$(brew --prefix)}"

# Clean previous builds
rm -rf "${DIST_DIR}" "${ROOT_DIR}/build"

# Run PyInstaller
pyinstaller "${SPEC_FILE}"

# Copy GTK stack and other Homebrew libraries into the bundle
mkdir -p "${FRAMEWORKS_DIR}" "${RES_DIR}/lib/girepository-1.0" "${RES_DIR}/lib/gdk-pixbuf-2.0/2.10.0/loaders" "${RES_DIR}/share"

LIBS=(gtk4 libadwaita gdk-pixbuf glib pango cairo harfbuzz graphene pcre2 fribidi gobject-introspection)
for pkg in "${LIBS[@]}"; do
  if [ -d "${BREW_PREFIX}/opt/${pkg}/lib" ]; then
    cp "${BREW_PREFIX}/opt/${pkg}/lib"/*.dylib "${FRAMEWORKS_DIR}/" 2>/dev/null || true
  fi
done

# GI typelibs
if [ -d "${BREW_PREFIX}/lib/girepository-1.0" ]; then
  cp "${BREW_PREFIX}/lib/girepository-1.0"/*.typelib "${RES_DIR}/lib/girepository-1.0/" 2>/dev/null || true
fi

# gdk-pixbuf loaders
if [ -d "${BREW_PREFIX}/lib/gdk-pixbuf-2.0/2.10.0/loaders" ]; then
  cp "${BREW_PREFIX}/lib/gdk-pixbuf-2.0/2.10.0/loaders"/*.so "${RES_DIR}/lib/gdk-pixbuf-2.0/2.10.0/loaders/" 2>/dev/null || true
  if command -v gdk-pixbuf-query-loaders >/dev/null 2>&1; then
    gdk-pixbuf-query-loaders "${RES_DIR}/lib/gdk-pixbuf-2.0/2.10.0/loaders"/*.so > "${RES_DIR}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
  fi
fi

# Copy icons and schemas
if [ -d "${BREW_PREFIX}/share/icons" ]; then
  cp -R "${BREW_PREFIX}/share/icons" "${RES_DIR}/share/"
fi
if [ -d "${BREW_PREFIX}/share/glib-2.0/schemas" ]; then
  mkdir -p "${RES_DIR}/share/glib-2.0"
  cp "${BREW_PREFIX}/share/glib-2.0/schemas"/*.xml "${RES_DIR}/share/glib-2.0/schemas/" 2>/dev/null || true
fi

# Re-sign bundle with ad-hoc signature for distribution
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "${APP_BUNDLE}" >/dev/null 2>&1 || true
fi

echo "✅ sshPilot.app created at ${APP_BUNDLE} with all dependencies bundled"
