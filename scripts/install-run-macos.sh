#!/usr/bin/env bash
set -euo pipefail

# One-shot installer/runner for sshPilot on macOS.
# - Installs Homebrew (if missing)
# - Installs GTK4/libadwaita/VTE stack and tools
# - Clones the repo (or updates it)
# - Creates a venv and installs Python deps
# - Launches the app with the correct environment

REPO_URL="https://github.com/mfat/sshpilot.git"
# If running from a local repo copy, prefer that instead of cloning
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -f "$ROOT_DIR/sshpilot/main.py" ]]; then
  DEFAULT_DIR="$ROOT_DIR"
else
  DEFAULT_DIR="$HOME/sshpilot"
fi
TARGET_DIR="${1:-$DEFAULT_DIR}"

echo "[1/6] Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ "$(uname -m)" == "arm64" ]]; then
    eval "$($(dirname $(dirname $(which brew)))/bin/brew shellenv)" || true
  else
    eval "$($(dirname $(dirname $(which brew)))/bin/brew shellenv)" || true
  fi
fi

BREW_PREFIX="$(brew --prefix)"
export PATH="$BREW_PREFIX/bin:$PATH"

echo "[2/6] Installing system packages (GTK stack, VTE, tools)..."
brew update
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass || true

echo "[3/6] Fetching sshPilot source into: $TARGET_DIR"
if [[ "$TARGET_DIR" != "$ROOT_DIR" ]]; then
  if [[ -d "$TARGET_DIR/.git" ]]; then
    git -C "$TARGET_DIR" pull --rebase --autostash || true
  else
    git clone "$REPO_URL" "$TARGET_DIR"
  fi
else
  echo "Using local repository at $TARGET_DIR"
fi

cd "$TARGET_DIR"

echo "[4/6] Creating Python virtualenv (with system-site-packages) and installing deps..."
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
"$PYTHON_BIN" -m venv .venv --system-site-packages
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt || true

# Ensure macOS-friendly secret storage fallback when running against upstream clone
if ! python -c "import secretstorage" >/dev/null 2>&1; then
  if grep -q "^import secretstorage$" sshpilot/connection_manager.py 2>/dev/null; then
    echo "Patching secretstorage import for macOS..."
    python - <<'PY'
from pathlib import Path
p = Path('sshpilot/connection_manager.py')
s = p.read_text()
s = s.replace('import secretstorage', 'try:\n    import secretstorage\nexcept Exception:\n    secretstorage = None')
p.write_text(s)
print('Patched', p)
PY
  fi
fi

echo "[5/6] Writing run wrapper (scripts/run-macos.sh)..."
mkdir -p scripts
cat > scripts/run-macos.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
fi

BREW_PREFIX="$(brew --prefix)"
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/opt/gtk4/lib:$BREW_PREFIX/opt/glib/lib:$BREW_PREFIX/opt/vte3/lib:$BREW_PREFIX/opt/icu4c/lib:$BREW_PREFIX/opt/graphene/lib:$BREW_PREFIX/lib"
export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
export XDG_DATA_DIRS="$BREW_PREFIX/share"

exec python run.py
EOF
chmod +x scripts/run-macos.sh

echo "[6/6] Launching sshPilot..."
exec scripts/run-macos.sh


