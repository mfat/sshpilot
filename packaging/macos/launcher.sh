#!/bin/bash
set -euo pipefail

# Determine bundle paths
HERE="$(cd "$(dirname "$0")" && pwd)"
CONTENTS="${HERE}/.."
RES="${CONTENTS}/Resources"
LIB_DIR="${RES}/lib"

# GTK/GLib/GObject Introspection paths relative to bundle (per official docs)
export GI_TYPELIB_PATH="${LIB_DIR}/girepository-1.0"
export GSETTINGS_SCHEMA_DIR="${RES}/share/glib-2.0/schemas"
export XDG_DATA_DIRS="${RES}/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GTK_DATA_PREFIX="${RES}"
export GTK_EXE_PREFIX="${RES}"
export GTK_PATH="${RES}"

# macOS dynamic loader fallbacks to bundled libraries first
export DYLD_FALLBACK_LIBRARY_PATH="${LIB_DIR}:${DYLD_FALLBACK_LIBRARY_PATH:-}"

# Themes and integration
export GTK_ICON_THEME="Adwaita"
export XDG_ICON_THEME="Adwaita"
export GTK_USE_PORTAL="1"
export GTK_CSD="1"

# Set PYTHONPATH to include the app directory so modules can be found
export PYTHONPATH="${RES}/app:${PYTHONPATH:-}"

# Prefer bundled python site-packages if present
if [ -d "${LIB_DIR}/python3/site-packages" ]; then
  export PYTHONPATH="${LIB_DIR}/python3/site-packages:${PYTHONPATH}"
fi

# Create a temporary package directory to handle relative imports
TMP_PKG_DIR="$(mktemp -d)"
cp -r "${RES}/app"/* "${TMP_PKG_DIR}/"

# Change to the temporary package directory
cd "${TMP_PKG_DIR}"

# Create a simple launcher that runs the app as a module
cat > launcher_temp.py << 'EOF'
#!/usr/bin/env python3
import sys
import os

# Add current directory to path
sys.path.insert(0, os.getcwd())

# Create a simple main function that imports and runs the app
def run_app():
    try:
        # Import the main function directly
        import main
        main.main()
    except ImportError as e:
        print(f"Import error: {e}")
        print("Current directory:", os.getcwd())
        print("Python path:", sys.path)
        sys.exit(1)
    except Exception as e:
        print(f"Runtime error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    run_app()
EOF

# Launch the app using the temporary launcher
exec /usr/bin/env python3 launcher_temp.py
