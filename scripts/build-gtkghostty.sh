#!/bin/sh
# Build libghostty-vt and the vendored gtkghostty widget (subprojects/gtkghostty)
# and install both into PREFIX, system-wide, for bundles that collect their
# typelibs from the system (the AppImage). The deb, rpm and Arch packages use
# the meson option gtkghostty instead, which installs a private copy.
#
#   sudo scripts/build-gtkghostty.sh /usr
#
# libghostty-vt comes ready-made (packaging/windows/libghostty-vt/build.py
# --prebuilt): on Linux the archive gtkghostty's CI builds and tests, on
# macOS Ghostty's official XCFramework, both pinned by checksum; no Zig. It
# needs network access.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
prefix=${1:?usage: $0 PREFIX}

python3 "$root/packaging/windows/libghostty-vt/build.py" --prebuilt --prefix "$prefix"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
PKG_CONFIG_PATH="$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
    meson setup "$work/build" "$root/subprojects/gtkghostty" --prefix="$prefix" --buildtype=release
meson compile -C "$work/build"
meson install -C "$work/build"
if [ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ] && command -v ldconfig >/dev/null; then
    ldconfig
fi
