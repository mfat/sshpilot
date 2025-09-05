# hook-gtk_runtime.py  (packaging file, not app source)
import os, sys
from pathlib import Path

if sys.platform == "darwin":
    # Find .../Contents robustly from the running executable
    cur = Path(sys.executable).resolve()
    for _ in range(8):
        if (cur / "Contents").exists():
            contents = cur / "Contents"; break
        cur = cur.parent
    else:
        contents = Path(getattr(sys, "_MEIPASS", Path.cwd())) / ".."

    resources  = (contents / "Resources").resolve()
    frameworks = (contents / "Frameworks").resolve()

    gi_paths = [
        str(resources / "girepository-1.0"),
        str(resources / "gi_typelibs"),  # PyInstaller’s GI dump
    ]
    os.environ["GI_TYPELIB_PATH"] = ":".join([p for p in gi_paths if Path(p).exists()])
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(resources / "share" / "glib-2.0" / "schemas")
    os.environ["XDG_DATA_DIRS"] = str(resources / "share")
    
    # Add bundled sshpass to PATH
    bundled_bin = str(frameworks / "Resources" / "bin")
    if Path(bundled_bin).exists():
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bundled_bin}:{current_path}"
        print(f"DEBUG: Added bundled bin to PATH: {bundled_bin}")
    
    # Add GI modules to Python path for Cairo bindings
    gi_modules_path = str(frameworks / "gi")
    if Path(gi_modules_path).exists():
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        if current_pythonpath:
            os.environ["PYTHONPATH"] = f"{gi_modules_path}:{current_pythonpath}"
        else:
            os.environ["PYTHONPATH"] = gi_modules_path
        print(f"DEBUG: Added GI modules to PYTHONPATH: {gi_modules_path}")
    
    # Include both Frameworks and Frameworks/Frameworks for libraries
    fallback_paths = [str(frameworks), str(frameworks / "Frameworks")]
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(fallback_paths)
    # Also set DYLD_LIBRARY_PATH for GObject Introspection
    os.environ["DYLD_LIBRARY_PATH"] = ":".join(fallback_paths)
    print(f"DEBUG: DYLD_FALLBACK_LIBRARY_PATH = {os.environ['DYLD_FALLBACK_LIBRARY_PATH']}")
    print(f"DEBUG: DYLD_LIBRARY_PATH = {os.environ['DYLD_LIBRARY_PATH']}")
    print(f"DEBUG: frameworks = {frameworks}")
    print(f"DEBUG: frameworks/Frameworks = {frameworks / 'Frameworks'}")
    print(f"DEBUG: resources = {resources}")
    print(f"DEBUG: GI_TYPELIB_PATH = {os.environ.get('GI_TYPELIB_PATH', 'NOT SET')}")
    os.environ.pop("LD_LIBRARY_PATH", None)
