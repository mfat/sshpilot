# sshpilot-windows.spec: the Windows build (python.org Python + gvsbuild GTK 4).
#
#   powershell -File packaging\windows\build.ps1
#
# One Analysis of run.py, two executables in one folder:
#   sshPilot.exe      windowed: the GUI
#   sshpilot-cli.exe  console:  the daemon (--daemon), askpass
#                     (--internal-askpass) and rbw's pinentry
#                     (--internal-rbw-pinentry). A windowed exe has no console
#                     to hand OpenSSH's children, and no stdout for helpers.
# Bundled beside them: the patched libvterm-0.dll, and Microsoft's conpty.dll
# with its OpenConsole.exe (both from vendor/, see docs/running-on-windows.md).

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]
VENDOR = ROOT / "vendor"
ARCH = "x64"

entry_py = str(ROOT / "run.py")
icon_file = ROOT / "packaging" / "windows" / "sshpilot.ico"


def _require(path: Path, hint: str) -> str:
    if not path.is_file():
        raise SystemExit(f"missing {path}: {hint}")
    return str(path)


binaries = [
    (_require(VENDOR / "libvterm" / ARCH / "libvterm-0.dll",
              "run packaging/windows/libvterm/build.py --compiler msvc"), "."),
    (_require(VENDOR / "conpty" / ARCH / "conpty.dll", "run scripts/fetch-conpty.py"), "."),
    (_require(VENDOR / "conpty" / ARCH / "OpenConsole.exe", "run scripts/fetch-conpty.py"), "."),
]
datas = [
    (str(ROOT / "src" / "sshpilot" / "resources"), "sshpilot/resources"),
    (str(ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / "io.github.mfat.sshpilot.svg"), "share/icons"),
]
datas += collect_data_files("sshpilot", includes=["**/*.gresource", "**/*.ui", "**/*.css", "**/*.svg", "**/*.json", "**/*.mo", "**/*.js", "**/*.html"])
datas += collect_data_files("sshpilot.plugins.builtin", includes=["**/plugin.json"])
for name in ("keyring", "certifi", "cryptography"):
    try:
        datas += copy_metadata(name)
    except Exception as exc:  # optional metadata
        print(f"WARNING: no metadata for {name}: {exc}")
try:
    datas += collect_data_files("certifi")
except Exception:
    pass

hiddenimports = collect_submodules("gi")
hiddenimports += ["gi._gi_cairo", "gi.repository.cairo", "cairo"]
hiddenimports += ["sshpilot.daemon.cli", "sshpilot.daemon.askpass_helper"]
hiddenimports += collect_submodules("sshpilot")
hiddenimports += ["keyring", "keyring.backends", "keyring.backends.Windows", "win32ctypes", "win32ctypes.core"]
hiddenimports += ["certifi", "cryptography", "yaml", "psutil", "pyte"]
for optional in ("pykeepass", "lxml", "Cryptodome", "argon2"):
    try:
        hiddenimports += collect_submodules(optional)
        binaries += collect_dynamic_libs(optional)
    except Exception:
        pass

gi_hooksconfig = {
    "gi": {
        "module-versions": {"Gtk": "4.0", "Gdk": "4.0", "GtkSource": "5", "Adw": "1"},
        "icons": ["Adwaita", "hicolor"],
        "themes": ["Adwaita"],
    },
}

a = Analysis(
    [entry_py],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hooksconfig=gi_hooksconfig,
    # VTE, WebKitGTK and the Unix-only modules do not exist on Windows.
    excludes=["gi.repository.Vte", "gi.repository.WebKit", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

common = dict(
    exclude_binaries=True,
    icon=str(icon_file) if icon_file.is_file() else None,
    upx=False,
    version=None,
)
gui = EXE(pyz, a.scripts, [], name="sshPilot", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="sshpilot-cli", console=True, **common)

coll = COLLECT(
    gui,
    cli,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="sshPilot",
)
