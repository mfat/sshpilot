# hook-gtk_runtime.py
import os, sys
from pathlib import Path

if sys.platform == "darwin":
    # In a frozen app, _MEIPASS points into Contents/MacOS
    app_root = Path(getattr(sys, "_MEIPASS", Path.cwd()))
    resources = (app_root / ".." / "Resources").resolve()
    frameworks = (app_root / ".." / "Frameworks").resolve()

    share = resources / "share"
    gi_repo = resources / "girepository-1.0"

    os.environ["GI_TYPELIB_PATH"] = str(gi_repo)
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(share / "glib-2.0" / "schemas")

    # Make themes/icons/data visible
    xdg = str(share)
    if os.environ.get("XDG_DATA_DIRS"):
        xdg = f"{share}:{os.environ['XDG_DATA_DIRS']}"
    os.environ["XDG_DATA_DIRS"] = xdg

    # Help the dynamic loader find vendored dylibs
    fallback = str(frameworks)
    if os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"):
        fallback = f"{frameworks}:{os.environ['DYLD_FALLBACK_LIBRARY_PATH']}"
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = fallback
    
    # Set DYLD_LIBRARY_PATH to find VTE and other libraries
    dylib_path = str(frameworks)
    if os.environ.get("DYLD_LIBRARY_PATH"):
        dylib_path = f"{frameworks}:{os.environ['DYLD_LIBRARY_PATH']}"
    os.environ["DYLD_LIBRARY_PATH"] = dylib_path
