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
INIT_FILE="sshpilot/__init__.py"
RPM_SPEC_FILE="packaging/fedora/rpm.spec"
METAINFO_FILE="io.github.mfat.sshpilot.metainfo.xml"
DEB_CHANGELOG="debian/changelog"
# Series named in the committed changelog entry. Informational only: the
# Launchpad recipe re-targets each build to its actual series, but the
# version ({VERSION}-0ubuntu1) is what the recipe's {debupstream} reads.
DEB_SERIES="resolute"

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

CURRENT_VERSION=$(python3 - "$INIT_FILE" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
print(match.group(1) if match else "0.0.0")
PY
)
echo "Current version: $CURRENT_VERSION"

read -rp "New version (semver, e.g. 2.1.0): " VERSION
VERSION=${VERSION#v}
VERSION=${VERSION#V}
if [[ -z "${VERSION}" ]]; then
  echo "ERROR: Version is required." >&2
  exit 1
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
  echo "ERROR: '$VERSION' does not look like semver (X.Y.Z)." >&2
  exit 1
fi

python3 - "$CURRENT_VERSION" "$VERSION" <<'PY'
import re
import sys


def parse_version(value: str):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-.*)?$", value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


current = parse_version(sys.argv[1])
new = parse_version(sys.argv[2])
if current is None or new is None:
    raise SystemExit("ERROR: Could not parse version numbers for comparison.")
if new <= current:
    raise SystemExit(
        f"ERROR: New version {sys.argv[2]} must be greater than current {sys.argv[1]}."
    )
PY

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

python3 - "$INIT_FILE" "$VERSION" <<'PY'
import re
import sys
from pathlib import Path

path, version = sys.argv[1], sys.argv[2]
text = Path(path).read_text(encoding="utf-8")
pattern = re.compile(r"""^(__version__\s*=\s*['"])([^'"]+)(['"].*)$""", re.M)

def repl(match):
    return f"{match.group(1)}{version}{match.group(3)}"


new_text = pattern.sub(repl, text)
if new_text == text:
    raise SystemExit(f"ERROR: Could not update __version__ in {path}")
Path(path).write_text(new_text, encoding="utf-8")
PY

if [[ -f "$RPM_SPEC_FILE" ]]; then
  echo "Updating version in $RPM_SPEC_FILE..."
  python3 - "$RPM_SPEC_FILE" "$VERSION" "$CHANGELOG" <<'PY'
import re
import sys
from datetime import datetime
from pathlib import Path

path, version, changelog = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(path).read_text(encoding="utf-8")

version_pattern = re.compile(r"^(Version:\s+%\{\?version\}%\{!\?version:)([^}]+)(\}.*)$", re.M)
text = version_pattern.sub(lambda match: f"{match.group(1)}{version}{match.group(3)}", text)

def normalize_bullet(line):
    line = re.sub(r"^[-*]\s*", "", line.strip())
    return f"- {line}" if line else None


changelog_lines = [
    item
    for item in (normalize_bullet(line) for line in changelog.strip().split("\n"))
    if item
]
today = datetime.now().strftime("%a %b %d %Y")
new_entry = (
    f"* {today} mFat <newmfat@gmail.com> - {version}\n"
    + "\n".join(changelog_lines)
    + "\n\n"
)
text = re.sub(r"(%changelog\s*\n)", r"\1" + new_entry, text, count=1, flags=re.M)
Path(path).write_text(text, encoding="utf-8")
PY
else
  echo "WARNING: $RPM_SPEC_FILE not found, skipping RPM spec update." >&2
fi

if [[ ! -f "$METAINFO_FILE" ]]; then
  echo "ERROR: $METAINFO_FILE not found." >&2
  exit 1
fi

python3 - "$METAINFO_FILE" "$VERSION" "$CHANGELOG" <<'PY'
import html
import re
import sys
from datetime import datetime
from pathlib import Path

path, version, changelog = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(path).read_text(encoding="utf-8")


def normalize_line(line):
    line = re.sub(r"^[-*]\s*", "", line.strip())
    return html.escape(line) if line else None


changelog_lines = [
    item
    for item in (normalize_line(line) for line in changelog.strip().split("\n"))
    if item
]
description_content = "\n".join(f"        <p>{line}</p>" for line in changelog_lines)
today = datetime.now().strftime("%Y-%m-%d")
new_release = (
    f'    <release version="{version}" date="{today}">\n'
    f"      <description>\n"
    f"{description_content}\n"
    f"      </description>\n"
    f"    </release>\n"
)
text = re.sub(r"(<releases>\s*\n)", r"\1" + new_release, text, count=1, flags=re.M)
Path(path).write_text(text, encoding="utf-8")
PY

echo "Updating $DEB_CHANGELOG..."
export DEBFULLNAME="Mehdi" DEBEMAIL="mah.fat@gmail.com"
dch -c "$DEB_CHANGELOG" -v "${VERSION}-0ubuntu1" -D "$DEB_SERIES" \
  --force-distribution "Release v${VERSION}."
while IFS= read -r line; do
  line="$(sed 's/^[[:space:]]*[-*]*[[:space:]]*//' <<<"$line")"
  [[ -n "$line" ]] && dch -c "$DEB_CHANGELOG" -a "$line"
done <<<"$CHANGELOG"

if ! grep -qE "from \. import __version__\s+as\s+APP_VERSION" sshpilot/window.py; then
  echo "WARNING: About dialog may not reflect __version__ automatically. Please verify in sshpilot/window.py." >&2
fi

git add "$INIT_FILE" "$METAINFO_FILE" "$DEB_CHANGELOG"
if [[ -f "$RPM_SPEC_FILE" ]]; then
  git add "$RPM_SPEC_FILE"
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

echo
echo "Release v$VERSION pushed."
echo "Monitor GitHub Actions for build progress."
