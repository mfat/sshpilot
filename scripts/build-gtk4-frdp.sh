#!/bin/sh
# Build and install gtk4-frdp (the GTK 4 RDP widget behind RDP tabs) for the
# AppImage and macOS bundles, from the copy in subprojects/gtk4-frdp
# (scripts/update-gtk4-frdp.sh). The deb, rpm, Arch and Flatpak builds compile
# the same copy through sshPilot's own Meson build (option rdp_tab).
#
# Usage: scripts/build-gtk4-frdp.sh PREFIX [extra meson setup options...]
# Needs FreeRDP 3 (freerdp3/winpr3 pkg-config files), GTK 4, meson, ninja and
# gobject-introspection.
set -eu

src=$(cd "$(dirname "$0")/../subprojects/gtk4-frdp" && pwd)
prefix=${1:?usage: $0 PREFIX [meson options...]}
shift

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

meson setup "$work/build" "$src" \
    --prefix="$prefix" --buildtype=release \
    -Dtests=false -Dexamples=false -Ddocumentation=false "$@"
meson compile -C "$work/build"
meson install -C "$work/build"

# The AppImage build resolves typelib libraries through ldconfig's cache.
if [ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ] && command -v ldconfig >/dev/null; then
    ldconfig
fi
