#!/bin/bash

# Get the app bundle root directory
APP="$(cd "$(dirname "$0")/.."; pwd -P)"/..
RES="$APP/Contents/Resources"

# Set up Python environment (use system Python with bundled site-packages)
export PYTHONPATH="$RES/app:$RES/lib/python3.13/site-packages:$RES/lib/python3.12/site-packages:${PYTHONPATH:-}"

# GTK/GLib/GObject Introspection paths (bundled inside app)
export GI_TYPELIB_PATH="$RES/lib/girepository-1.0"
export GSETTINGS_SCHEMA_DIR="$RES/share/glib-2.0/schemas"
export XDG_DATA_DIRS="$RES/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

# GTK-specific paths
export GTK_DATA_PREFIX="$RES"
export GTK_EXE_PREFIX="$RES"
export GTK_PATH="$RES"

# macOS loader paths (bundled libraries)
export DYLD_LIBRARY_PATH="$RES/lib"
export DYLD_FALLBACK_LIBRARY_PATH="$RES/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"

# Icon and theme settings
export GTK_ICON_THEME="Adwaita"
export XDG_ICON_THEME="Adwaita"
export GTK_THEME=""
export GTK_USE_PORTAL="1"
export GTK_CSD="1"

# Change to the Resources directory and run from there
cd "$RES"

# Start the application using system Python with bundled libraries
# Run from the parent directory so the package can be imported properly
exec python3 -c "
import sys
import os
sys.path.insert(0, os.path.join(os.getcwd(), 'app'))
from app.main import main
main()
"
