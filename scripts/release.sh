#!/usr/bin/env bash

set -euo pipefail

# Interactive front-end for the Release workflow (.github/workflows/release.yml).
# Collects the version/changelog and dispatches the workflow, which owns the
# whole release: bump (scripts/bump-version.sh), merge dev into main, tag,
# GitHub release, Arch PKGBUILD, and the Launchpad PPA nudge.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEFAULT_DEV_BRANCH="dev"
DEFAULT_MAIN_BRANCH="main"
WORKFLOW="release.yml"

echo "sshPilot release helper"
echo

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh (GitHub CLI) is required. Install: https://cli.github.com/" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated. Run: gh auth login" >&2
  exit 1
fi

read -rp "Dev branch to release from [$DEFAULT_DEV_BRANCH]: " DEV_BRANCH
DEV_BRANCH=${DEV_BRANCH:-$DEFAULT_DEV_BRANCH}
read -rp "Main branch to merge into [$DEFAULT_MAIN_BRANCH]: " MAIN_BRANCH
MAIN_BRANCH=${MAIN_BRANCH:-$DEFAULT_MAIN_BRANCH}

read -rp "Publish as [R]elease or [P]re-release? [R]: " RELEASE_KIND
RELEASE_KIND=${RELEASE_KIND:-R}
case "$RELEASE_KIND" in
  [Rr]|[Rr]elease) PRERELEASE=false ;;
  [Pp]|[Pp]re|[Pp]re-release|[Pp]rerelease) PRERELEASE=true ;;
  *)
    echo "ERROR: Enter R (release) or P (pre-release)." >&2
    exit 1
    ;;
esac

read -rp "New version (semver, e.g. 2.1.0): " VERSION
VERSION=${VERSION#v}
VERSION=${VERSION#V}
if [[ -z "$VERSION" ]]; then
  echo "ERROR: Version is required." >&2
  exit 1
fi

echo
echo "Enter changelog for v$VERSION (plain lines; do not prefix with '-')."
echo "Leave empty to let the workflow derive it from the commits. End with Ctrl-D:"
# `cat` rather than a `read -e` loop: readline/bracketed-paste mangling dropped
# every line but the first when pasting multi-line notes.
CHANGELOG="$(cat)"

echo
echo "Ready to dispatch the Release workflow:"
echo "  Version:      v$VERSION"
echo "  Branches:     $DEV_BRANCH -> $MAIN_BRANCH"
echo "  Pre-release:  $PRERELEASE"
if [[ -z "${CHANGELOG//[[:space:]]/}" ]]; then
  echo "  Changelog:    (derived from commits since the last release)"
else
  echo "  Changelog:"
  sed 's/^/    /' <<<"$CHANGELOG"
fi
echo
read -rp "Dispatch? [y/N]: " CONFIRM
CONFIRM=${CONFIRM:-N}
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Aborted; nothing was dispatched."
  exit 1
fi

# Dispatch the workflow as it exists on the dev branch, so a release picks up
# workflow changes that have not been merged to main yet (the release itself
# does that merge).
gh workflow run "$WORKFLOW" --ref "$DEV_BRANCH" \
  -f "version=$VERSION" \
  -f "changelog=$CHANGELOG" \
  -f "prerelease=$PRERELEASE" \
  -f "dev_branch=$DEV_BRANCH" \
  -f "main_branch=$MAIN_BRANCH"

# The dispatch API returns nothing; poll for the run it created.
echo "Waiting for the run to appear..."
RUN_ID=""
for _ in $(seq 1 10); do
  sleep 3
  RUN_ID=$(gh run list --workflow "$WORKFLOW" --branch "$DEV_BRANCH" \
    --limit 1 --json databaseId,status \
    --jq '.[0] | select(.status != "completed") | .databaseId' || true)
  [[ -n "$RUN_ID" ]] && break
done
if [[ -z "$RUN_ID" ]]; then
  echo "Could not find the dispatched run; check: gh run list --workflow $WORKFLOW" >&2
  exit 1
fi

echo "Watching run $RUN_ID (Ctrl-C detaches; the release keeps running)..."
gh run watch "$RUN_ID" --exit-status

echo
echo "Release v$VERSION dispatched and finished."
echo "The .deb/.rpm/DMG builds run off the pushed tag; monitor GitHub Actions."
if [[ "$PRERELEASE" == "true" ]]; then
  echo "APT / Homebrew / Flathub updates are skipped for pre-releases."
else
  echo "A Flathub manifest-bump PR opens on flathub/io.github.mfat.sshpilot; merge it to publish."
fi
