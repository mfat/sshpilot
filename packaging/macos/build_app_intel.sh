#!/usr/bin/env bash
# Build sshPilot macOS app bundle and DMG for Intel (x86_64) Macs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
arch -x86_64 bash "${SCRIPT_DIR}/build_app.sh" "$@"
