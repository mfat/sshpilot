#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
fi

# Try to find brew
BREW_PREFIX=""
if command -v brew >/dev/null 2>&1; then
  BREW_PREFIX="$(brew --prefix)"
elif [[ -d /opt/homebrew ]]; then
  BREW_PREFIX="/opt/homebrew"
elif [[ -d /usr/local ]]; then
  BREW_PREFIX="/usr/local"
fi

if [[ -n "$BREW_PREFIX" && -d "$BREW_PREFIX" ]]; then
  export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/opt/gtk4/lib:$BREW_PREFIX/opt/glib/lib:$BREW_PREFIX/opt/vte3/lib:$BREW_PREFIX/opt/icu4c/lib:$BREW_PREFIX/opt/graphene/lib:$BREW_PREFIX/lib"
  export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
  export XDG_DATA_DIRS="$BREW_PREFIX/share"
fi

exec python3 run.py

