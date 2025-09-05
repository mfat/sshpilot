#!/usr/bin/env python3
"""
Debug script to test the runtime hook
"""

import os, sys
from pathlib import Path

print("=== Runtime Hook Debug ===")
print(f"sys.platform: {sys.platform}")
print(f"sys._MEIPASS: {getattr(sys, '_MEIPASS', 'Not set')}")
print(f"Current working directory: {Path.cwd()}")

if sys.platform == "darwin":
    app_root = Path(getattr(sys, "_MEIPASS", Path.cwd()))
    print(f"app_root: {app_root}")
    
    resources  = (app_root / ".." / "Resources").resolve()
    frameworks = (app_root / ".." / "Frameworks").resolve()
    
    print(f"resources: {resources}")
    print(f"frameworks: {frameworks}")
    
    print(f"resources exists: {resources.exists()}")
    print(f"frameworks exists: {frameworks.exists()}")
    
    if resources.exists():
        print(f"resources contents: {list(resources.iterdir())[:10]}")
    
    if frameworks.exists():
        print(f"frameworks contents: {list(frameworks.iterdir())[:10]}")
    
    # Check for GResource file
    gresource_path = resources / "venv" / "lib" / "python3.13" / "site-packages" / "sshpilot" / "resources" / "sshpilot.gresource"
    print(f"GResource path: {gresource_path}")
    print(f"GResource exists: {gresource_path.exists()}")
    
    # Check for typelibs
    typelib_path = resources / "girepository-1.0"
    print(f"Typelib path: {typelib_path}")
    print(f"Typelib exists: {typelib_path.exists()}")
    
    if typelib_path.exists():
        print(f"Typelib contents: {list(typelib_path.iterdir())[:5]}")

print("=== Environment Variables ===")
for var in ['GI_TYPELIB_PATH', 'GSETTINGS_SCHEMA_DIR', 'XDG_DATA_DIRS', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_LIBRARY_PATH']:
    print(f"{var}: {os.environ.get(var, 'Not set')}")
