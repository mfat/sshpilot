#!/usr/bin/env bash
# End-to-end #1384 check on NixOS: store-backed ~/.ssh/config + Flatpak ownership.
#
# Run on a NixOS host (or inside an Incus/QEMU NixOS VM). Optional: import the
# minimal ISO from ~/Downloads into a VM first; this script does not install NixOS.
#
# Usage (on NixOS):
#   ./scripts/validate-nixos-flatpak-ssh.sh
#   ./scripts/validate-nixos-flatpak-ssh.sh --with-sshpilot   # needs flatpak remote
#
# Usage (host launches Incus VM, then runs checks inside):
#   ./scripts/validate-nixos-flatpak-ssh.sh --incus [instance-name]
#
set -euo pipefail
cd "$(dirname "$0")/.."

ISO_DEFAULT="$HOME/Downloads/nixos-minimal-26.05.14630.f68407657ad2-x86_64-linux.iso"
INCUS_NAME="${SSHPILOT_NIX_VM:-sshpilot-nix-repro}"
WITH_SSHPILOT=0
MODE=local

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-sshpilot) WITH_SSHPILOT=1; shift ;;
    --incus)
      MODE=incus
      shift
      [[ $# -gt 0 && "$1" != --* ]] && INCUS_NAME="$1" && shift
      ;;
    -h|--help)
      sed -n '1,22p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

run_checks() {
  cat <<'REMOTE'
set -euo pipefail
if ! command -v nix-build >/dev/null 2>&1; then
  echo "This script must run on NixOS (needs nix-build)." >&2
  exit 1
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cat >"$WORKDIR/ssh-store.nix" <<'NIX'
{ pkgs }:
pkgs.writeText "hm_.sshconfig" ''
  Host nixrepro
    HostName 127.0.0.1
    User nixuser
''
NIX

STORE_PATH=$(nix-build --no-out-link "$WORKDIR/ssh-store.nix" 2>/dev/null | tail -1)
mkdir -p "$HOME/.ssh"
ln -sf "$STORE_PATH" "$HOME/.ssh/config"

echo "=== Host ==="
stat -Lc 'owner=%u group=%g mode=%a path=%n' "$HOME/.ssh/config"
readlink -f "$HOME/.ssh/config" | xargs stat -Lc 'target owner=%u group=%g'

if ! command -v flatpak >/dev/null 2>&1; then
  echo "flatpak not installed; skipping sandbox section (install via nixpkgs)." >&2
  exit 0
fi

APP_ID="${SSHPILOT_FLATPAK_ID:-io.github.mfat.sshpilot}"
if flatpak info "$APP_ID" >/dev/null 2>&1; then
  echo
  echo "=== Flatpak sandbox: $APP_ID ==="
  flatpak run --command=sh "$APP_ID" -c '
    id
    stat -Lc "owner=%u group=%g mode=%a" "$HOME/.ssh/config"
    cat /proc/self/uid_map
    PYTHONPATH=/app/lib/python*/site-packages:/app/lib/python3.14/site-packages \
      python3 -c "
from sshpilot.core.ssh_config_effective import (
    direct_f_workaround_for_unmapped_user_ssh_config,
    launch_config_file,
)
from sshpilot.platform.paths import openssh_user_dir
root = openssh_user_dir() / \"config\"
print(\"workaround\", direct_f_workaround_for_unmapped_user_ssh_config(str(root)))
print(\"launch_f\", launch_config_file(str(root)))
" 2>/dev/null || echo "(dev build modules not in flatpak runtime; use ssh -F below)"
    /app/bin/ssh -G nixrepro 2>&1 | head -6 || true
  '
else
  echo "Flatpak app $APP_ID not installed; grant /nix/store:ro in Flatseal after install."
fi
REMOTE
}

case "$MODE" in
  incus)
    if ! command -v incus >/dev/null 2>&1; then
      echo "incus not found" >&2
      exit 1
    fi
    if ! incus info "$INCUS_NAME" >/dev/null 2>&1; then
      IMAGE="images:nixos/26.05"
      echo "Creating Incus VM $INCUS_NAME from $IMAGE"
      echo "ISO available at: $ISO_DEFAULT"
      incus launch "$IMAGE" "$INCUS_NAME" --vm -c limits.memory=3GiB -c limits.cpu=2
      incus exec "$INCUS_NAME" -- bash -c 'nix-env -iA nixos.flatpak 2>/dev/null || true'
    fi
    run_checks | incus exec "$INCUS_NAME" -- bash -s
    ;;
  local)
    if [[ -f /etc/NIXOS ]]; then
      run_checks | bash -s
    else
      echo "Not on NixOS. Use: $0 --incus"
      echo "Or boot the minimal ISO in a VM:"
      echo "  $ISO_DEFAULT"
      exit 1
    fi
    ;;
esac