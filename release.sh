#!/usr/bin/env bash
set -euo pipefail

# sshPilot Release Helper
# - Reads version from sshpilot/__init__.py
# - Creates git tag v<version> and pushes branch + tag
# - Optionally builds .deb and creates a GitHub release (if gh CLI is available)

usage() {
  cat <<EOF
Usage: $(basename "$0") [--no-build] [--notes NOTES_FILE] [--dry-run]

Options:
  --no-build      Skip building the Debian package
  --notes FILE    Use a custom release notes file instead of auto-generating from git log
  --dry-run       Show what would happen without executing push/release steps

Requirements:
  - Clean git working tree
  - Version defined in sshpilot/__init__.py as __version__ = "X.Y.Z"
  - GitHub CLI 'gh' (optional) for creating releases and uploading assets
EOF
}

NO_BUILD=0
NOTES_FILE=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) NO_BUILD=1; shift ;;
    --notes) NOTES_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# Ensure clean tree
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: Working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

# Determine version from package
VERSION=$(python3 -c "import re, pathlib; t=pathlib.Path('sshpilot/__init__.py').read_text(); m=re.search(r'__version__\\s*=\\s*\"([^\"]+)\"', t); print(m.group(1) if m else '')")
if [[ -z "$VERSION" ]]; then
  echo "Error: Could not parse __version__ from sshpilot/__init__.py" >&2
  exit 1
fi

TAG="v${VERSION}"
BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "Preparing release ${TAG} on branch ${BRANCH}"

# Check tag
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Error: Tag ${TAG} already exists." >&2
  exit 1
fi

# Generate release notes
if [[ -n "$NOTES_FILE" ]]; then
  if [[ ! -f "$NOTES_FILE" ]]; then
    echo "Error: notes file not found: $NOTES_FILE" >&2
    exit 1
  fi
  NOTES=$(cat "$NOTES_FILE")
else
  LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
  if [[ -n "$LAST_TAG" ]]; then
    NOTES=$(git log --pretty=format:'- %s' "$LAST_TAG"..HEAD)
  else
    NOTES=$(git log --pretty=format:'- %s')
  fi
fi

# Build Debian package (optional)
DEB_ASSET=""
if [[ "$NO_BUILD" -eq 0 ]]; then
  if [[ -x ./build-deb.sh ]]; then
    echo "Building Debian package..."
    ./build-deb.sh
    DEB_ASSET=$(ls -1 ../sshpilot_${VERSION}-1_*.deb 2>/dev/null | head -n1 || true)
  else
    echo "Skipping build: build-deb.sh not found or not executable" >&2
  fi
else
  echo "Skipping build as requested (--no-build)."
fi

echo "Creating tag ${TAG}"
if [[ "$DRY_RUN" -eq 0 ]]; then
  git tag -a "$TAG" -m "Release ${TAG}"
  git push origin "$BRANCH"
  git push origin "$TAG"
else
  echo "DRY RUN: git tag -a ${TAG} -m 'Release ${TAG}'"
  echo "DRY RUN: git push origin ${BRANCH} && git push origin ${TAG}"
fi

# Create GitHub release via gh CLI if available
if command -v gh >/dev/null 2>&1; then
  echo "Creating GitHub release ${TAG}"
  GH_ARGS=(release create "$TAG" -t "sshPilot ${TAG}" -n "$NOTES")
  if [[ -n "$DEB_ASSET" && -f "$DEB_ASSET" ]]; then
    GH_ARGS+=("$DEB_ASSET")
  fi
  if [[ "$DRY_RUN" -eq 0 ]]; then
    gh "${GH_ARGS[@]}"
  else
    echo "DRY RUN: gh ${GH_ARGS[*]}"
  fi
else
  cat <<EONOTE
Note: 'gh' CLI not found. Git tag has been created and pushed.
You can create a release manually at: https://github.com/<owner>/sshpilot/releases/new?tag=${TAG}
Suggested release notes:

${NOTES}
EONOTE
fi

echo "Release ${TAG} complete."


