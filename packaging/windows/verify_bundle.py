#!/usr/bin/env python3
"""Check a built Windows app folder holds what the app loads at runtime.

PyInstaller does not see ctypes loads, and its GI hooks skip typelibs
silently when GIRepository cannot find them, so a bundle can build and then
fail on first launch. This fails the build instead.

    python packaging/windows/verify_bundle.py build/windows/dist/sshPilot
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

REQUIRED_FILES = [
    "sshPilot.exe",
    "sshpilot-cli.exe",
    "_internal/libvterm-0.dll",
    "_internal/conpty.dll",
    "_internal/OpenConsole.exe",
    # gtk4-frdp and what it loads, for RDP connections in a tab.
    "_internal/gtk4-frdp-4.0-0.dll",
    "_internal/freerdp3.dll",
    "_internal/freerdp-client3.dll",
    "_internal/winpr3.dll",
    "_internal/libssl-3-x64.dll",
    "_internal/libcrypto-3-x64.dll",
    "_internal/share/licenses/gtk4-frdp/NOTICE",
]
REQUIRED_TYPELIBS = ["Gtk-4.0", "Gdk-4.0", "Adw-1", "GtkSource-5", "Pango-1.0", "GdkPixbuf-2.0", "Gio-2.0", "GtkFrdp-4.0"]
REQUIRED_GLOBS = [
    ("GSettings schemas", "_internal/share/glib-2.0/schemas/gschemas.compiled"),
    ("pixbuf loaders cache", "_internal/lib/gdk-pixbuf/loaders.cache"),
    ("app resources", "_internal/sshpilot/resources/sshpilot.gresource"),
]


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:] or ["build/windows/dist/sshPilot"])[0])
    problems = []
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            problems.append(f"missing {relative}")
    typelib_dirs = list(root.glob("_internal/**/girepository-1.0")) + list(root.glob("_internal/gi_typelibs"))
    found = {path.stem for directory in typelib_dirs for path in directory.glob("*.typelib")}
    for name in REQUIRED_TYPELIBS:
        if name not in found:
            problems.append(f"missing typelib {name}")
    for label, pattern in REQUIRED_GLOBS:
        if not list(root.glob(pattern)):
            problems.append(f"missing {label} ({pattern})")
    if problems:
        print("Bundle check failed:\n  " + "\n  ".join(problems))
        return 1
    if sys.platform == "win32":
        check = Path(__file__).resolve().parent / "check-rdp-crypto.py"
        result = subprocess.run([sys.executable, str(check), str(root / "_internal"),
                                 str(root / "_internal")])
        if result.returncode:
            print("Bundle check failed: RDP MD4/RC4 support unavailable")
            return 1
    size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    print(f"Bundle OK: {root} ({size / (1 << 20):.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
