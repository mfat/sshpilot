"""PyInstaller runtime hook for GTK4 on Windows.

This hook sets up the environment for GTK4 to find its resources in the
bundled application directory.
"""

import os
import sys

# Get the directory where the bundled app is located
if getattr(sys, 'frozen', False):
    bundle_dir = sys._MEIPASS
else:
    bundle_dir = os.path.dirname(os.path.abspath(__file__))

# Set up GTK environment variables
os.environ['GTK_DATA_PREFIX'] = bundle_dir
os.environ['GTK_EXE_PREFIX'] = bundle_dir
os.environ['GDK_PIXBUF_MODULEDIR'] = os.path.join(bundle_dir, 'lib', 'gdk-pixbuf-2.0', '2.10.0', 'loaders')
os.environ['GDK_PIXBUF_MODULE_FILE'] = os.path.join(bundle_dir, 'lib', 'gdk-pixbuf-2.0', '2.10.0', 'loaders.cache')

# Add share directory to XDG_DATA_DIRS
share_dir = os.path.join(bundle_dir, 'share')
if os.path.exists(share_dir):
    xdg_data_dirs = os.environ.get('XDG_DATA_DIRS', '')
    if xdg_data_dirs:
        os.environ['XDG_DATA_DIRS'] = f"{share_dir}{os.pathsep}{xdg_data_dirs}"
    else:
        os.environ['XDG_DATA_DIRS'] = share_dir

# Set GI typelib path
gi_typelib_path = os.path.join(bundle_dir, 'lib', 'girepository-1.0')
if os.path.exists(gi_typelib_path):
    os.environ['GI_TYPELIB_PATH'] = gi_typelib_path

# Set PATH to include bundled DLLs
bin_dir = bundle_dir
if bin_dir not in os.environ.get('PATH', ''):
    os.environ['PATH'] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

# Disable GTK's inspector by default (can be re-enabled for debugging)
if 'GTK_DEBUG' not in os.environ:
    os.environ['GTK_DEBUG'] = ''

# On Windows, ensure we use the bundled schemas
schemas_dir = os.path.join(share_dir, 'glib-2.0', 'schemas')
if os.path.exists(schemas_dir):
    os.environ['GSETTINGS_SCHEMA_DIR'] = schemas_dir

print(f"GTK runtime hook initialized for Windows")
print(f"Bundle dir: {bundle_dir}")
print(f"GI_TYPELIB_PATH: {os.environ.get('GI_TYPELIB_PATH', 'not set')}")
print(f"XDG_DATA_DIRS: {os.environ.get('XDG_DATA_DIRS', 'not set')}")
