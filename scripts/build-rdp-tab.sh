#!/bin/sh
# Build the bundled gtk4-frdp (subprojects/gtk4-frdp) into build/rdp-tab, so
# running sshPilot from a checkout (python3 run.py) opens RDP connections in a
# tab. Packaged builds compile it themselves (meson option rdp_tab); this is
# only for source runs. Re-run after scripts/update-gtk4-frdp.sh.
#
# Needs FreeRDP 3 >= 3.11 development files, GTK 4, meson, ninja and
# gobject-introspection (Debian/Ubuntu: freerdp3-dev libwinpr3-dev
# libgirepository1.0-dev; Fedora: freerdp-devel gobject-introspection-devel).
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
out=$root/build/rdp-tab

rm -rf "$out"
meson setup "$out/meson" "$root/subprojects/gtk4-frdp" \
    --prefix="$out" --libdir=lib --buildtype=release \
    -Dprivate_libdir=sshpilot \
    -Dtests=false -Dexamples=false -Ddocumentation=false
meson compile -C "$out/meson"
meson install --quiet -C "$out/meson"
echo "gtk4-frdp built into $out/lib/sshpilot; run sshPilot from this checkout to use it."
