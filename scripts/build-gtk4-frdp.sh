#!/bin/sh
# Build and install gtk4-frdp (the GTK 4 RDP widget behind RDP tabs) for the
# AppImage and macOS bundles. Distro packages depend on the gtk4-frdp package
# instead, and the Flatpak manifest builds it as a module.
#
# Usage: scripts/build-gtk4-frdp.sh PREFIX [extra meson setup options...]
# Needs FreeRDP 3 (freerdp3/winpr3 pkg-config files), GTK 4, meson, ninja,
# git and gobject-introspection. Keep GTK4_FRDP_COMMIT in step with the
# commit in io.github.mfat.sshpilot.yaml and packaging/windows.
set -eu

GTK4_FRDP_URL=${GTK4_FRDP_URL:-https://github.com/mfat/gtk4-frdp.git}
GTK4_FRDP_COMMIT=${GTK4_FRDP_COMMIT:-2f4d96d199b06d94f88bb893450473433ec7b514}

prefix=${1:?usage: $0 PREFIX [meson options...]}
shift

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

git init -q "$work/src"
git -C "$work/src" fetch -q --depth 1 "$GTK4_FRDP_URL" "$GTK4_FRDP_COMMIT"
git -C "$work/src" checkout -q FETCH_HEAD

meson setup "$work/build" "$work/src" \
    --prefix="$prefix" --buildtype=release \
    -Dtests=false -Dexamples=false -Ddocumentation=false "$@"
meson compile -C "$work/build"
meson install -C "$work/build"

# The AppImage build resolves typelib libraries through ldconfig's cache.
if [ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ] && command -v ldconfig >/dev/null; then
    ldconfig
fi
