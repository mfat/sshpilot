#!/usr/bin/env bash

set -euo pipefail

# Interactive release helper for sshPilot
# - Bumps version on dev and pushes dev
# - Merges dev into main, tags, and pushes main + tag
# - GitHub Actions builds assets and publishes the GitHub release

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEFAULT_DEV_BRANCH="dev"
DEFAULT_MAIN_BRANCH="main"
# The files carrying the version/changelog, and the rules for writing them,
# live in scripts/bump-version.sh -- shared with the Release workflow.
BUMP_SCRIPT="scripts/bump-version.sh"
PKGBUILD_SCRIPT="scripts/update-arch-pkgbuild.sh"
INIT_FILE="src/sshpilot/__init__.py"
RPM_SPEC_FILE="packaging/fedora/rpm.spec"
METAINFO_FILE="data/io.github.mfat.sshpilot.metainfo.xml.in"
PKGBUILD_FILE="packaging/ArchLinux/PKGBUILD"
DEB_CHANGELOG="debian/changelog"

echo "sshPilot release helper"
echo

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is not installed." >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: This is not a git repository." >&2
  exit 1
fi

if ! command -v dch >/dev/null 2>&1; then
  echo "ERROR: dch is required to update debian/changelog (the Launchpad PPA" >&2
  echo "recipe reads its version from it). Install with: sudo apt install devscripts" >&2
  exit 1
fi

if ! git diff-index --quiet HEAD -- || ! git diff --quiet; then
  echo "ERROR: Working tree has uncommitted changes. Please commit or stash before releasing." >&2
  echo "Run 'git status' to see what files have changes." >&2
  exit 1
fi

if [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "WARNING: There are untracked files in the working directory." >&2
  echo "Run 'git status' to see untracked files." >&2
  read -rp "Continue anyway? [y/N]: " CONTINUE_WITH_UNTRACKED
  CONTINUE_WITH_UNTRACKED=${CONTINUE_WITH_UNTRACKED:-N}
  if [[ ! "$CONTINUE_WITH_UNTRACKED" =~ ^[Yy]$ ]]; then
    echo "Aborting release." >&2
    exit 1
  fi
fi

read -rp "Dev branch to release from [$DEFAULT_DEV_BRANCH]: " DEV_BRANCH
DEV_BRANCH=${DEV_BRANCH:-$DEFAULT_DEV_BRANCH}
read -rp "Main branch to merge into [$DEFAULT_MAIN_BRANCH]: " MAIN_BRANCH
MAIN_BRANCH=${MAIN_BRANCH:-$DEFAULT_MAIN_BRANCH}

git fetch --all --tags --prune
if ! git rev-parse --verify "$DEV_BRANCH" >/dev/null 2>&1; then
  echo "ERROR: Branch '$DEV_BRANCH' does not exist." >&2
  exit 1
fi
if ! git rev-parse --verify "$MAIN_BRANCH" >/dev/null 2>&1; then
  echo "ERROR: Branch '$MAIN_BRANCH' does not exist." >&2
  exit 1
fi

git checkout "$DEV_BRANCH"
git pull --ff-only origin "$DEV_BRANCH"

echo
echo "Latest commit on $DEV_BRANCH:"
git --no-pager log -1 --oneline
echo

if [[ ! -f "$INIT_FILE" ]]; then
  echo "ERROR: $INIT_FILE not found." >&2
  exit 1
fi

CURRENT_VERSION=$(sed -n "s/^__version__ *= *['\"]\\([^'\"]*\\)['\"].*/\\1/p" "$INIT_FILE")
echo "Current version: ${CURRENT_VERSION:-unknown}"

read -rp "New version (semver, e.g. 2.1.0): " VERSION
VERSION=${VERSION#v}
VERSION=${VERSION#V}
if [[ -z "${VERSION}" ]]; then
  echo "ERROR: Version is required." >&2
  exit 1
fi
# Validate before asking for the changelog, with the same rules that will be
# applied to the files below.
"$BUMP_SCRIPT" --check "$VERSION" >/dev/null

if git rev-parse "v$VERSION" >/dev/null 2>&1; then
  echo "ERROR: Tag v$VERSION already exists." >&2
  exit 1
fi

echo
echo "Enter changelog for v$VERSION (plain lines; do not prefix with '-'):"
echo "End with Ctrl-D:"
# Read the whole changelog from stdin until EOF (Ctrl-D). Using `cat` rather
# than a `read -e` loop avoids readline/bracketed-paste mangling that dropped
# every line but the first when pasting multi-line notes.
CHANGELOG="$(cat)"
if [[ -z "${CHANGELOG}" ]]; then
  echo "WARNING: Empty changelog."
fi

printf '%s\n' "$CHANGELOG" | "$BUMP_SCRIPT" "$VERSION"

if ! grep -qE "from \. import __version__\s+as\s+APP_VERSION" src/sshpilot/window.py; then
  echo "WARNING: About dialog may not reflect __version__ automatically. Please verify in src/sshpilot/window.py." >&2
fi

# -f: debian/ may still be ignored in older local checkouts; force-add is safe
# for already-tracked packaging files and avoids set -e aborting on Git ≥2.25.
git add -f "$INIT_FILE" "$METAINFO_FILE" "$DEB_CHANGELOG"
if [[ -f "$RPM_SPEC_FILE" ]]; then
  git add -f "$RPM_SPEC_FILE"
fi
git commit -m "Bump version to $VERSION"

echo "Pushing version bump to origin/$DEV_BRANCH..."
git push origin "$DEV_BRANCH"

git checkout "$MAIN_BRANCH"
git pull --ff-only origin "$MAIN_BRANCH"

echo "Merging $DEV_BRANCH into $MAIN_BRANCH..."
if ! git merge --no-ff "$DEV_BRANCH" -m "Merge $DEV_BRANCH into $MAIN_BRANCH for v$VERSION"; then
  echo "Merge conflicts detected. Resolving by preferring dev branch version..."
  CONFLICTED_FILES=$(git diff --name-only --diff-filter=U)
  for file in $CONFLICTED_FILES; do
    echo "Resolving conflict in $file (preferring dev branch version)..."
    git checkout --theirs "$file"
    git add "$file"
  done
  git commit --no-edit
  echo "Conflicts resolved successfully."
fi

git tag -a "v$VERSION" -m "SSH Pilot v$VERSION" -m "$CHANGELOG"

echo
echo "Ready to publish v$VERSION:"
echo "  Current version was: $CURRENT_VERSION"
echo "  Dev branch pushed:   origin/$DEV_BRANCH"
echo "  Merge commit on:     $MAIN_BRANCH"
echo "  Tag to push:         v$VERSION"
echo "  CI will build .deb/.rpm/DMG packages, publish the GitHub release, and update APT/Homebrew."
echo "  A Flathub manifest-bump PR is opened automatically on flathub/io.github.mfat.sshpilot; merge it to publish to Flathub."
echo
read -rp "Push $MAIN_BRANCH and tag v$VERSION to origin? [y/N]: " CONFIRM_PUSH
CONFIRM_PUSH=${CONFIRM_PUSH:-N}
if [[ ! "$CONFIRM_PUSH" =~ ^[Yy]$ ]]; then
  echo "Aborted before pushing main/tag. Local branches and tag were kept."
  echo "Resume manually with:"
  echo "  git push origin $MAIN_BRANCH"
  echo "  git push origin v$VERSION"
  exit 1
fi

git push origin "$MAIN_BRANCH"
git push origin "v$VERSION"

# The Arch PKGBUILD is updated here, after the tag exists, because its
# sha256sums cover GitHub's generated tag tarball — which cannot be hashed
# before the tag is pushed. It is only ever touched on $MAIN_BRANCH (never on
# $DEV_BRANCH) so the merge above can never conflict over it.
if [[ -f "$PKGBUILD_FILE" ]]; then
  echo
  echo "Updating $PKGBUILD_FILE for v$VERSION..."
  # Non-fatal: the tag is already pushed, so a transient download failure must
  # not abort the run. The script prints how to finish it by hand.
  if "$PKGBUILD_SCRIPT" "$VERSION"; then
    git add "$PKGBUILD_FILE"
    git commit -m "Update Arch PKGBUILD for v$VERSION"
    git push origin "$MAIN_BRANCH"
  fi
fi

echo
echo "Release v$VERSION pushed."
echo "Monitor GitHub Actions for build progress."
