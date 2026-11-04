#!/bin/sh
# Refresh the copy of gtk4-frdp in subprojects/gtk4-frdp.
#
#   scripts/update-gtk4-frdp.sh <tag-or-commit>
#
# The deb, rpm and Arch packages build gtk4-frdp from this copy (meson option
# rdp_tab) and install it privately under <libdir>/sshpilot, so RDP connections
# open in a tab without a separate gtk4-frdp package. It is vendored rather
# than fetched because Launchpad and COPR build without network access.
# Only what the build needs is kept: the library sources and the licences.
set -eu

REF=${1:?usage: $0 <tag-or-commit>}
URL=https://github.com/mfat/gtk4-frdp
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST=$ROOT/subprojects/gtk4-frdp

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git -C "$TMP" init -q
git -C "$TMP" fetch -q --depth 1 "$URL" "$REF"
COMMIT=$(git -C "$TMP" rev-parse FETCH_HEAD)
git -C "$TMP" archive --prefix=gtk4-frdp/ FETCH_HEAD \
    COPYING NOTICE LICENSES README.md NEWS.md meson.build meson_options.txt src \
    | tar -x -C "$TMP"

rm -rf "$DEST"
mkdir -p "$ROOT/subprojects"
mv "$TMP/gtk4-frdp" "$DEST"
printf '%s\n%s\n' "$URL" "$COMMIT" > "$DEST/VENDORED"
echo "subprojects/gtk4-frdp now at $COMMIT"
