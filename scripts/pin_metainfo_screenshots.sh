#!/usr/bin/env bash
# Pin io.github.mfat.sshpilot.metainfo.xml screenshot URLs to a git commit.
#
# Usage (after committing new PNGs under screenshots/):
#   ./scripts/pin_metainfo_screenshots.sh          # uses HEAD
#   ./scripts/pin_metainfo_screenshots.sh <commit> # explicit commit

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
METAINFO_FILE="$ROOT_DIR/data/io.github.mfat.sshpilot.metainfo.xml"
COMMIT="${1:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"

if [[ ! -f "$METAINFO_FILE" ]]; then
  echo "ERROR: $METAINFO_FILE not found." >&2
  exit 1
fi

if ! git -C "$ROOT_DIR" cat-file -e "${COMMIT}:screenshots/start-page.png" 2>/dev/null; then
  echo "ERROR: Commit $COMMIT does not contain screenshots/start-page.png." >&2
  echo "Commit your screenshot PNGs first, then re-run this script." >&2
  exit 1
fi

sed -i "s|https://raw.githubusercontent.com/mfat/sshpilot/[^/]*/screenshots/|https://raw.githubusercontent.com/mfat/sshpilot/${COMMIT}/screenshots/|g" \
  "$METAINFO_FILE"

echo "Pinned metainfo screenshot URLs to $COMMIT"
