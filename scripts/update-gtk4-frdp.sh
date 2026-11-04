#!/bin/sh
# Update the gtk4-frdp that sshPilot ships, everywhere, to a release tag.
#
#   scripts/update-gtk4-frdp.sh vX.Y.Z
#
# 1. Replaces the copy in subprojects/gtk4-frdp with that tag's source. The
#    deb, rpm, Arch, Flatpak, AppImage and macOS builds compile it (meson
#    option rdp_tab, scripts/build-gtk4-frdp.sh). It is vendored rather than
#    fetched because Launchpad and COPR build without network access. Only
#    what the build needs is kept: the library sources and the licences.
# 2. Pins the Windows build (scripts/fetch-gtk.py) to the same release's
#    prebuilt zip, which gtk4-frdp's CI attaches to the tag. Tag, wait for
#    that CI run, then run this.
#
# tests/test_gtk4_frdp_pin.py fails when the two disagree.
#
#   scripts/update-gtk4-frdp.sh --commit <full-sha>
#
# vendors an untagged commit instead, for trying a change before release; it
# leaves the Windows pin alone, and the test then fails until a tag is used.
set -eu

URL=https://github.com/mfat/gtk4-frdp
REPO=mfat/gtk4-frdp
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST=$ROOT/subprojects/gtk4-frdp
FETCH_GTK=$ROOT/scripts/fetch-gtk.py

usage() { echo "usage: $0 vX.Y.Z | --commit <full-sha>" >&2; exit 2; }

TAG=
if [ "${1:-}" = --commit ]; then
    REF=${2:-}; [ -n "$REF" ] || usage
else
    TAG=${1:-}
    case $TAG in v[0-9]*.[0-9]*.[0-9]*) ;; *) usage ;; esac
    REF=refs/tags/$TAG
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# The Windows zip first: if the release has none yet, change nothing.
if [ -n "$TAG" ]; then
    VERSION=${TAG#v}
    GVSBUILD=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$FETCH_GTK")
    ASSET=gtk4-frdp-$VERSION-gvsbuild-$GVSBUILD-x64.zip
    if ! curl -fsSL -o "$TMP/$ASSET" "$URL/releases/download/$TAG/$ASSET"; then
        echo "error: $TAG has no $ASSET yet." >&2
        echo "Its CI attaches it when the windows-gvsbuild job finishes:" >&2
        echo "  gh run list -R $REPO --branch $TAG" >&2
        exit 1
    fi
    SHA256=$(sha256sum "$TMP/$ASSET" | cut -d' ' -f1)
    # The zip's own checksum file, published by the same CI job.
    if curl -fsSL -o "$TMP/$ASSET.sha256" "$URL/releases/download/$TAG/$ASSET.sha256"; then
        PUBLISHED=$(cut -d' ' -f1 "$TMP/$ASSET.sha256" | tr 'A-F' 'a-f')
        if [ "$PUBLISHED" != "$SHA256" ]; then
            echo "error: $ASSET checksum $SHA256 does not match the published $PUBLISHED" >&2
            exit 1
        fi
    fi
fi

git -C "$TMP" init -q src
git -C "$TMP/src" fetch -q --depth 1 "$URL" "$REF"
COMMIT=$(git -C "$TMP/src" rev-parse 'FETCH_HEAD^{commit}')
git -C "$TMP/src" archive --prefix=gtk4-frdp/ FETCH_HEAD \
    COPYING NOTICE LICENSES README.md NEWS.md meson.build meson_options.txt src \
    | tar -x -C "$TMP"

rm -rf "$DEST"
mkdir -p "$ROOT/subprojects"
mv "$TMP/gtk4-frdp" "$DEST"
# url / commit / tag ("" for an untagged commit); read by the pin test.
printf '%s\n%s\n%s\n' "$URL" "$COMMIT" "$TAG" > "$DEST/VENDORED"
echo "subprojects/gtk4-frdp now at ${TAG:-$COMMIT} ($COMMIT)"

if [ -n "$TAG" ]; then
    sed -i -e "s/^FRDP_VERSION = \".*\"$/FRDP_VERSION = \"$VERSION\"/" \
           -e "s/^FRDP_SHA256 = \".*\"$/FRDP_SHA256 = \"$SHA256\"/" "$FETCH_GTK"
    echo "scripts/fetch-gtk.py now pins $ASSET ($SHA256)"
else
    echo "Windows pin unchanged (scripts/fetch-gtk.py); tag a release before shipping."
fi
echo "Run scripts/build-rdp-tab.sh to use it from this checkout."
