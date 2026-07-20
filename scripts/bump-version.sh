#!/usr/bin/env bash

set -euo pipefail

# Write a new version + changelog into every file that carries them:
# src/sshpilot/__init__.py, meson.build, the RPM spec, the AppStream metainfo
# and debian/changelog.
#
#   scripts/bump-version.sh <version> < changelog.txt
#   scripts/bump-version.sh --check <version>     # validate only, edit nothing
#
# Non-interactive and git-free on purpose: it only edits files, so both the
# interactive release helper (scripts/release.sh) and the Release workflow can
# drive it without either owning a second copy of these rules.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

INIT_FILE="src/sshpilot/__init__.py"
RPM_SPEC_FILE="packaging/fedora/rpm.spec"
METAINFO_FILE="data/io.github.mfat.sshpilot.metainfo.xml.in"
DEB_CHANGELOG="debian/changelog"
# Series named in the committed changelog entry. Informational only: the
# Launchpad recipe re-targets each build to its actual series, but the
# version ({VERSION}-0ubuntu1) is what the recipe's {debupstream} reads.
DEB_SERIES="resolute"

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
  shift
fi

VERSION="${1:-}"
VERSION=${VERSION#v}
VERSION=${VERSION#V}
if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 [--check] <version> < changelog" >&2
  exit 1
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
  echo "ERROR: '$VERSION' does not look like semver (X.Y.Z)." >&2
  exit 1
fi

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

if (( CHECK_ONLY )); then
  echo "$CURRENT_VERSION"
  exit 0
fi

if ! command -v dch >/dev/null 2>&1; then
  echo "ERROR: dch is required to update $DEB_CHANGELOG (the Launchpad PPA" >&2
  echo "recipe reads its version from it). Install with: sudo apt install devscripts" >&2
  exit 1
fi

CHANGELOG="$(cat)"
if [[ -z "${CHANGELOG// }" ]]; then
  echo "WARNING: Empty changelog." >&2
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

# Keep the Meson project() version in sync with __init__.py.
if [[ -f "meson.build" ]]; then
  echo "Updating version in meson.build..."
  python3 - "meson.build" "$VERSION" <<'PY'
import re, sys
from pathlib import Path
path, version = sys.argv[1], sys.argv[2]
text = Path(path).read_text(encoding="utf-8")
new = re.sub(r"(version:\s*')[^']+(',)", rf"\g<1>{version}\g<2>", text, count=1)
if new == text:
    raise SystemExit(f"ERROR: Could not update version in {path}")
Path(path).write_text(new, encoding="utf-8")
PY
fi

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
text, count = version_pattern.subn(
    lambda match: f"{match.group(1)}{version}{match.group(3)}", text
)
if not count:
    raise SystemExit(f"ERROR: Could not update Version in {path}")

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

echo "Updating $METAINFO_FILE..."
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
text, count = re.subn(r"(<releases>\s*\n)", r"\1" + new_release, text, count=1, flags=re.M)
if not count:
    raise SystemExit(f"ERROR: Could not insert a <release> into {path}")
Path(path).write_text(text, encoding="utf-8")
PY

# Man pages carry the version in their .TH header; keep them in step rather
# than letting them drift (they sat at 5.4.6 while the app was 5.5.7).
for man in data/sshpilot.1 data/sshpilot-agent.1; do
  if [[ -f "$man" ]]; then
    echo "Updating $man..."
    sed -i -E "s/^(\.TH [A-Z-]+ 1 )\"[^\"]*\" \"sshpilot [^\"]*\"/\\1\"$(date '+%B %Y')\" \"sshpilot ${VERSION}\"/" "$man"
  fi
done

echo "Updating $DEB_CHANGELOG..."
export DEBFULLNAME="${DEBFULLNAME:-Mehdi}" DEBEMAIL="${DEBEMAIL:-mah.fat@gmail.com}"
dch -c "$DEB_CHANGELOG" -v "${VERSION}-0ubuntu1" -D "$DEB_SERIES" \
  --force-distribution "Release v${VERSION}."
while IFS= read -r line; do
  line="$(sed 's/^[[:space:]]*[-*]*[[:space:]]*//' <<<"$line")"
  [[ -n "$line" ]] && dch -c "$DEB_CHANGELOG" -a "$line"
done <<<"$CHANGELOG"

echo "Bumped $CURRENT_VERSION -> $VERSION"
