#!/usr/bin/env bash
set -euo pipefail

# Build a self-contained sshPilot.app for macOS by bundling the Python app,
# GTK4/libadwaita/VTE GI runtime from Homebrew, and all required resources.
# Result goes to dist/sshPilot.app and a DMG in dist/sshPilot.dmg.

APP_NAME="sshPilot"
APP_ID="io.github.mfat.sshpilot"
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
BUILD_DIR="${ROOT_DIR}/build/macos"
BREW_PREFIX="$(brew --prefix)"

echo "Using Homebrew prefix: ${BREW_PREFIX}"
mkdir -p "${DIST_DIR}" "${BUILD_DIR}"

cd "${ROOT_DIR}"

python3 -m venv .venv-build --system-site-packages
source .venv-build/bin/activate
pip install --upgrade pip
pip install pyinstaller -r requirements.txt

# Use PyInstaller to produce a macOS app bundle skeleton
pyinstaller \
  --clean \
  --noconfirm \
  --windowed \
  --name "${APP_NAME}" \
  --osx-bundle-identifier "${APP_ID}" \
  run.py

APP_DIR="${ROOT_DIR}/dist/${APP_NAME}.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RES_DIR="${CONTENTS_DIR}/Resources"
FW_DIR="${CONTENTS_DIR}/Frameworks"

mkdir -p "${FW_DIR}" "${RES_DIR}/girepository-1.0" "${RES_DIR}/share" "${RES_DIR}/lib"

# Copy application resources
mkdir -p "${RES_DIR}/app"
rsync -a --exclude __pycache__ sshpilot "${RES_DIR}/app/"

# Include gresource bundle for runtime
if [ -f "${ROOT_DIR}/sshpilot/resources/sshpilot.gresource" ]; then
  mkdir -p "${RES_DIR}/app/sshpilot/resources"
  cp "${ROOT_DIR}/sshpilot/resources/sshpilot.gresource" "${RES_DIR}/app/sshpilot/resources/"
fi

# Bundle GI typelibs used at runtime
for typelib in Gtk-4.0 Adw-1 Vte-3.91 GdkPixbuf-2.0 Pango-1.0 Gio-2.0 GLib-2.0 GObject-2.0; do
  src="${BREW_PREFIX}/lib/girepository-1.0/${typelib}.typelib"
  if [ -f "$src" ]; then
    cp "$src" "${RES_DIR}/girepository-1.0/"
  fi
done

# Copy shared data needed by GTK/libadwaita/VTE
rsync -a "${BREW_PREFIX}/share/glib-2.0" "${RES_DIR}/share/" || true
rsync -a "${BREW_PREFIX}/share/icons" "${RES_DIR}/share/" || true
rsync -a "${BREW_PREFIX}/share/adwaita" "${RES_DIR}/share/" || true
rsync -a "${BREW_PREFIX}/share/gtk-4.0" "${RES_DIR}/share/" || true

# Copy GTK/VTE libraries (broad copy to ensure runtime completeness)
copy_lib() {
  local name="$1"
  local lib="$(ls -1 ${BREW_PREFIX}/lib/${name}* 2>/dev/null | head -n1 || true)"
  if [ -n "$lib" ]; then
    cp "$lib" "${FW_DIR}/"
  fi
}

copy_lib libgtk-4
copy_lib libadwaita-1
copy_lib libvte-2.91
copy_lib libpango-1.0
copy_lib libgdk_pixbuf-2.0
copy_lib libgio-2.0
copy_lib libgobject-2.0
copy_lib libglib-2.0
copy_lib libintl
copy_lib libffi
copy_lib libharfbuzz
copy_lib libgraphite2
copy_lib libcairo
copy_lib libpixman-1
copy_lib libpng16
copy_lib libfreetype
copy_lib libfontconfig
copy_lib libz

# Create launcher that sets env for bundled GI/GTK
LAUNCHER="${MACOS_DIR}/${APP_NAME}"
cat > "$LAUNCHER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONTENTS="${HERE}/.."
RES="${CONTENTS}/Resources"
FW="${CONTENTS}/Frameworks"

export DYLD_FALLBACK_LIBRARY_PATH="${FW}:${RES}/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"
export GI_TYPELIB_PATH="${RES}/girepository-1.0:${GI_TYPELIB_PATH:-}"
export XDG_DATA_DIRS="${RES}/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GTK_DATA_PREFIX="${RES}"
export GTK_EXE_PREFIX="${RES}"

exec "${HERE}/sshPilot" "$@"
EOF
chmod +x "$LAUNCHER"

# Ensure main binary exists (PyInstaller output name)
if [ ! -f "${MACOS_DIR}/sshPilot" ]; then
  echo "PyInstaller main binary not found; build may have failed" >&2
  exit 1
fi

# Optional: build a DMG
cd "${DIST_DIR}"
if command -v create-dmg >/dev/null 2>&1; then
  rm -f "${APP_NAME}.dmg"
  create-dmg --volname "${APP_NAME}" --overwrite "${APP_NAME}.dmg" "${APP_NAME}.app"
else
  echo "Tip: brew install create-dmg to produce a DMG. App bundle is ready at dist/${APP_NAME}.app"
fi

echo "Done. Open ${DIST_DIR}/${APP_NAME}.app"


