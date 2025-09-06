# hook-gtk_runtime.py  (packaging file, not app source)
import os, sys
from pathlib import Path

# Only emit debug output when explicitly enabled
debug = print if os.getenv("SSHPILOT_DEBUG") else lambda *a, **k: None

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
    
    # Set up keyring environment for macOS (like the working bundle)
    os.environ["KEYRING_BACKEND"] = "keyring.backends.macOS.Keyring"
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.macOS.Keyring"
    
    # Ensure keyring can access the user's keychain
    if "HOME" not in os.environ:
        os.environ["HOME"] = os.path.expanduser("~")
    if "USER" not in os.environ:
        os.environ["USER"] = os.environ.get("LOGNAME", "unknown")
    if "LOGNAME" not in os.environ:
        os.environ["LOGNAME"] = os.environ.get("USER", "unknown")
    if "SHELL" not in os.environ:
        os.environ["SHELL"] = "/bin/bash"
    
    # Critical for macOS keychain access (from working bundle)
    os.environ["KEYCHAIN_ACCESS_GROUP"] = "*"
    
    # Set up XDG directories for keyring
    home = os.environ["HOME"]
    os.environ["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    os.environ["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
    os.environ["XDG_CACHE_HOME"] = os.path.join(home, ".cache")
    
    # Create XDG directories if they don't exist
    for xdg_dir in ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"]:
        xdg_path = os.environ[xdg_dir]
        os.makedirs(xdg_path, exist_ok=True)
    
    # Set PATH explicitly for double-click launches (like working bundle)
    # This ensures the app has access to all necessary tools including system Python
    system_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = ":".join(system_paths + [current_path])
    
    # Add bundled sshpass to PATH
    bundled_bin = str(frameworks / "Resources" / "bin")
    if Path(bundled_bin).exists():
        os.environ["PATH"] = f"{bundled_bin}:{os.environ['PATH']}"
        debug(f"DEBUG: Added bundled bin to PATH: {bundled_bin}")
    
    # Add GI modules to Python path for Cairo bindings
    gi_modules_path = str(frameworks / "gi")
    if Path(gi_modules_path).exists():
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        if current_pythonpath:
            os.environ["PYTHONPATH"] = f"{gi_modules_path}:{current_pythonpath}"
        else:
            os.environ["PYTHONPATH"] = gi_modules_path
        debug(f"DEBUG: Added GI modules to PYTHONPATH: {gi_modules_path}")
    
    # Include both Frameworks and Frameworks/Frameworks for libraries
    fallback_paths = [str(frameworks), str(frameworks / "Frameworks")]
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(fallback_paths)
    # Also set DYLD_LIBRARY_PATH for GObject Introspection
    os.environ["DYLD_LIBRARY_PATH"] = ":".join(fallback_paths)
    debug(f"DEBUG: DYLD_FALLBACK_LIBRARY_PATH = {os.environ['DYLD_FALLBACK_LIBRARY_PATH']}")
    debug(f"DEBUG: DYLD_LIBRARY_PATH = {os.environ['DYLD_LIBRARY_PATH']}")
    debug(f"DEBUG: frameworks = {frameworks}")
    debug(f"DEBUG: frameworks/Frameworks = {frameworks / 'Frameworks'}")
    debug(f"DEBUG: resources = {resources}")
    debug(f"DEBUG: GI_TYPELIB_PATH = {os.environ.get('GI_TYPELIB_PATH', 'NOT SET')}")
    os.environ.pop("LD_LIBRARY_PATH", None)
