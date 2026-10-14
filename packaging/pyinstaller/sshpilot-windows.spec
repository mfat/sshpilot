# sshpilot-windows.spec — Windows PyInstaller build spec
# Build with: pyinstaller --clean sshpilot-windows.spec

import os
import sys
import glob
import platform
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs, copy_metadata

# This spec lives in packaging/pyinstaller/; anchor paths to the repo root.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir, os.pardir))

# Local helpers for GTK DLL placement (if needed for Windows)
sys.path.insert(0, SPECPATH)

app_name = "SSHPilot"
entry_py = os.path.join(ROOT, "run.py")
icon_file = os.path.join(ROOT, "packaging", "windows", "sshpilot.ico")  # TODO: create icon

# GTK4 on Windows is typically installed via MSYS2
# We need to bundle the GTK4 runtime DLLs and data files
MSYS2_PREFIX = os.environ.get('MSYS2_PREFIX', r'C:\msys64\mingw64')
MSYS2_BIN = os.path.join(MSYS2_PREFIX, 'bin')
MSYS2_LIB = os.path.join(MSYS2_PREFIX, 'lib')
MSYS2_SHARE = os.path.join(MSYS2_PREFIX, 'share')

binaries = []
datas = []

# GTK4 and related DLLs from MSYS2
# Note: PyInstaller's GTK hooks should collect these automatically,
# but we may need to help it find them on Windows
gtk_dll_patterns = [
    "libgtk-4-*.dll",
    "libgdk-4-*.dll",
    "libadwaita-*.dll",
    "libgdk_pixbuf-2.0-*.dll",
    "libglib-2.0-*.dll",
    "libgobject-2.0-*.dll",
    "libgio-2.0-*.dll",
    "libgmodule-2.0-*.dll",
    "libpango-1.0-*.dll",
    "libpangocairo-1.0-*.dll",
    "libcairo-2.dll",
    "libcairo-gobject-2.dll",
    "libharfbuzz-*.dll",
    "libfribidi-*.dll",
    "libgraphene-1.0-*.dll",
    "libgtksourceview-5-*.dll",
    "libintl-*.dll",
    "libffi-*.dll",
    "libpcre*.dll",
    "libiconv-*.dll",
    "libwinpthread-*.dll",
    "zlib*.dll",
    "libpng*.dll",
    "libjpeg-*.dll",
    "libgcc_*.dll",
    "libstdc++-*.dll",
    "libepoxy-*.dll",
]

# Collect GTK DLLs from MSYS2 bin directory
if os.path.exists(MSYS2_BIN):
    for pattern in gtk_dll_patterns:
        for dll in glob.glob(os.path.join(MSYS2_BIN, pattern)):
            binaries.append((dll, "."))
            print(f"Collected GTK DLL: {dll}")

# GI typelibs
gir_dir = os.path.join(MSYS2_LIB, "girepository-1.0")
if os.path.exists(gir_dir):
    for typelib in glob.glob(os.path.join(gir_dir, "*.typelib")):
        datas.append((typelib, "lib/girepository-1.0"))
        print(f"Collected typelib: {typelib}")

# GTK shared data
gtk_share_dirs = [
    ("glib-2.0/schemas", "share/glib-2.0/schemas"),
    ("icons/Adwaita", "share/icons/Adwaita"),
    ("gtk-4.0", "share/gtk-4.0"),
]

for src_subdir, dest_subdir in gtk_share_dirs:
    src_path = os.path.join(MSYS2_SHARE, src_subdir)
    if os.path.exists(src_path):
        datas.append((src_path, dest_subdir))
        print(f"Collected GTK data: {src_path} -> {dest_subdir}")

# libadwaita share data
libadwaita_share = os.path.join(MSYS2_SHARE, "libadwaita-1")
if os.path.exists(libadwaita_share):
    datas.append((libadwaita_share, "share/libadwaita-1"))
    print(f"Collected libadwaita data: {libadwaita_share}")

# gtksourceview5 share data
gtksourceview5_share = os.path.join(MSYS2_SHARE, "gtksourceview-5")
if os.path.exists(gtksourceview5_share):
    datas.append((gtksourceview5_share, "share/gtksourceview-5"))
    print(f"Collected gtksourceview5 data: {gtksourceview5_share}")

# GDK-Pixbuf loaders
gdkpixbuf_loaders = os.path.join(MSYS2_LIB, "gdk-pixbuf-2.0", "2.10.0")
if os.path.exists(gdkpixbuf_loaders):
    datas.append((gdkpixbuf_loaders, "lib/gdk-pixbuf-2.0/2.10.0"))
    print(f"Collected GDK-Pixbuf loaders: {gdkpixbuf_loaders}")

# Application resources
datas += [
    (os.path.join(ROOT, "src", "sshpilot"), "sshpilot"),
    (os.path.join(ROOT, "data", "icons", "hicolor", "scalable", "apps",
                  "io.github.mfat.sshpilot.svg"), "share/icons"),
]

# Keyring package files
try:
    import site
    site_packages = site.getsitepackages()[0]
    keyring_package = os.path.join(site_packages, "keyring")
    if os.path.exists(keyring_package):
        datas.append((keyring_package, "keyring"))
        print(f"Collected keyring package: {keyring_package}")
except Exception as exc:
    print(f"WARNING: could not collect keyring package: {exc}")

# Keyring metadata
try:
    datas += copy_metadata("keyring")
except Exception as exc:
    print(f"WARNING: could not copy keyring metadata: {exc}")

# Hidden imports
hiddenimports = collect_submodules("gi")
hiddenimports += ["gi._gi", "gi.repository.cairo", "cairo"]

# Daemon entrypoint (dispatched via --daemon)
hiddenimports += ["sshpilot.daemon.cli"]

# Force PyInstaller GTK hooks
hiddenimports += [
    "gi.repository.Gtk",
    "gi.repository.Gdk",
    "gi.repository.GtkSource",
    "gi.repository.Adw",
]

# Built-in plugins
hiddenimports += collect_submodules("sshpilot.plugins.builtin")
datas += collect_data_files("sshpilot.plugins.builtin", includes=["**/plugin.json"])

# Keyring and backends
hiddenimports += [
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",  # Windows Credential Manager
]

# certifi/cryptography for HTTPS
hiddenimports += ["certifi", "cryptography"]

# KeePass (.kdbx) support (optional)
for _kp_mod in ("pykeepass", "construct", "Cryptodome", "argon2", "argon2_cffi_bindings", "lxml"):
    try:
        hiddenimports += collect_submodules(_kp_mod)
    except Exception:
        pass

try:
    datas += collect_data_files("pykeepass")
except Exception:
    pass

for _kp_bin in ("lxml", "Cryptodome", "argon2_cffi_bindings"):
    try:
        binaries += collect_dynamic_libs(_kp_bin)
    except Exception:
        pass

# YAML for Ásbrú import
hiddenimports += ["yaml"]

# GI hooks configuration
gi_hooksconfig = {
    "gi": {
        "module-versions": {
            "Gtk": "4.0",
            "Gdk": "4.0",
            "GtkSource": "5",
        },
        "icons": ["Adwaita"],
        "themes": ["Adwaita"],
    },
}

block_cipher = None

a = Analysis(
    [entry_py],
    pathex=[os.path.join(ROOT, "src"), MSYS2_BIN],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[SPECPATH],
    hooksconfig=gi_hooksconfig,
    runtime_hooks=[os.path.join(SPECPATH, "hook-gtk_runtime_windows.py")],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI application, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file if os.path.exists(icon_file) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=app_name,
)
