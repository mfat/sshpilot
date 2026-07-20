#!/usr/bin/env bash

set -euo pipefail

# Point packaging/ArchLinux/PKGBUILD at a released tag.
#
#   scripts/update-arch-pkgbuild.sh <version>
#
# Must run *after* the tag is pushed: sha256sums covers the tarball GitHub
# generates for the tag, which does not exist before then. Edits the file only;
# the caller commits.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PKGBUILD_FILE="packaging/ArchLinux/PKGBUILD"
VERSION="${1:?Usage: $0 <version>}"
VERSION=${VERSION#v}

if [[ ! -f "$PKGBUILD_FILE" ]]; then
  echo "WARNING: $PKGBUILD_FILE not found, nothing to update." >&2
  exit 0
fi

TARBALL_URL="https://github.com/mfat/sshpilot/archive/refs/tags/v${VERSION}.tar.gz"
TARBALL="$(mktemp)"
trap 'rm -f "$TARBALL"' EXIT

SHA=""
for attempt in 1 2 3; do
  # GitHub generates the tarball on first request; give it a moment.
  if curl -sfL -o "$TARBALL" "$TARBALL_URL"; then
    SHA=$(sha256sum "$TARBALL" | cut -d' ' -f1)
    break
  fi
  echo "  tarball not ready yet (attempt $attempt), retrying..."
  sleep 5
done

if [[ -z "$SHA" ]]; then
  echo "ERROR: could not download $TARBALL_URL." >&2
  echo "Update $PKGBUILD_FILE by hand: set pkgver=$VERSION, pkgrel=1 and run updpkgsums." >&2
  exit 1
fi

sed -i -e "s/^pkgver=.*/pkgver=${VERSION}/" \
       -e "s/^pkgrel=.*/pkgrel=1/" \
       -e "s/^sha256sums=.*/sha256sums=('${SHA}')/" "$PKGBUILD_FILE"
echo "  pkgver=$VERSION, sha256sums=($SHA)"
