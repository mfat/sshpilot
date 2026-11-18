#!/usr/bin/env bash
# Reproduce Flatpak user-namespace ownership mapping (flatpak#6982) and the
# OpenSSH "Bad owner or permissions on ~/.ssh/config" failure that affects
# NixOS Home Manager layouts (sshpilot#1384).
#
# Usage:
#   ./scripts/repro-flatpak-6982.sh              # bubblewrap + optional flatpak
#   ./scripts/repro-flatpak-6982.sh --flatpak    # include io.github.mfat.sshpilot
#
set -euo pipefail

APP_ID="${SSHPILOT_FLATPAK_ID:-io.github.mfat.sshpilot}"
WITH_FLATPAK=0
for arg in "$@"; do
  case "$arg" in
    --flatpak) WITH_FLATPAK=1 ;;
    -h|--help)
      sed -n '1,12p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPRO="${TMPDIR:-/tmp}/flatpak-6982-repro"
STORE="/tmp/nix/store/flatpak6982-repro"
rm -rf "$REPRO" "$STORE"
mkdir -p "$REPRO/.ssh" "$STORE"

# Home Manager-style symlink chain to a root-owned, world-readable blob.
STAND_IN="${STAND_IN:-/etc/ssh/ssh_config}"
if [[ ! -r "$STAND_IN" ]]; then
  echo "Stand-in config $STAND_IN is missing; set STAND_IN to a root-owned file." >&2
  exit 1
fi
ln -sf "$STAND_IN" "$STORE/hm_.sshconfig"
ln -sf "$STORE/hm_.sshconfig" "$REPRO/.ssh/config"

echo "=== Host (outside any sandbox) ==="
stat -Lc 'owner=%u group=%g mode=%a path=%n' "$REPRO/.ssh/config"
readlink -f "$REPRO/.ssh/config" | xargs stat -Lc 'target owner=%u group=%g mode=%a'

if ! command -v bwrap >/dev/null 2>&1; then
  echo "bubblewrap (bwrap) not found; install bubblewrap to run the core repro." >&2
  exit 1
fi

echo
echo "=== bubblewrap (unmapped host root → UID 65534 in the sandbox) ==="
bwrap --unshare-user --uid "$(id -u)" --gid "$(id -g)" \
  --ro-bind / / \
  --bind "$REPRO" "$REPRO" \
  --proc /proc --dev /dev \
  --setenv HOME "$REPRO" \
  --chdir "$REPRO" \
  sh -c '
    id
    stat -Lc "owner=%u group=%g mode=%a" "$HOME/.ssh/config"
    cat /proc/self/uid_map
    echo "--- ssh -G localhost (default config, no -F) ---"
    if out=$(ssh -G localhost 2>&1 | head -8); then
      printf '%s\n' "$out"
    else
      printf '%s\n' "$out"
      echo "(ssh -G failed — expected when system snippets are root-owned)"
    fi
    echo "--- ssh -F \"\$HOME/.ssh/config\" -G localhost (workaround) ---"
    ssh -F "$HOME/.ssh/config" -G localhost 2>&1 | head -5 || true
  '

if [[ "$WITH_FLATPAK" -eq 1 ]]; then
  if ! command -v flatpak >/dev/null 2>&1; then
    echo "flatpak not installed; skipping app sandbox section." >&2
    exit 0
  fi
  if ! flatpak info "$APP_ID" >/dev/null 2>&1; then
    echo "Flatpak app $APP_ID is not installed; skipping." >&2
    exit 0
  fi
  echo
  echo "=== Flatpak: $APP_ID (distro-dependent id mapping) ==="
  flatpak run \
    --filesystem="$REPRO" \
    --filesystem="$STORE:ro" \
    --command=sh "$APP_ID" -c "
      export HOME=$REPRO
      id
      stat -Lc 'owner=%u group=%g mode=%a' \"\$HOME/.ssh/config\"
      cat /proc/self/uid_map
      /app/bin/ssh -G localhost 2>&1 | head -6 || true
    "
  echo
  echo "On NixOS + /nix/store:ro, stat here often shows owner=65534 and ssh"
  echo "without -F fails. Ubuntu may remap root-owned files to your UID instead."
fi

echo
echo "Done. Artefacts: $REPRO , $STORE"