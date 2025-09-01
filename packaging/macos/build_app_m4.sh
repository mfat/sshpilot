#!/usr/bin/env bash
# Build sshPilot macOS app bundle and DMG for Apple Silicon (arm64/M-series) Macs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
arch -arm64 bash "${SCRIPT_DIR}/build_app.sh" "$@"
