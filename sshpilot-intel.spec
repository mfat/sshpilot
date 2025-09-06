# sshpilot-intel.spec — build with: pyinstaller --clean sshpilot-intel.spec
# Fixed version for Intel Macs with proper Python dependency inclusion
import os, glob, platform
from PyInstaller.utils.hooks import collect_submodules

app_name = "SSHPilot"
entry_py = "run.py"
icon_file = "packaging/macos/sshpilot.icns"

# Intel Mac Homebrew path
homebrew = "/usr/local"
hb_lib = f"{homebrew}/lib"
hb_share = f"{homebrew}/share"
hb_gir = f"{hb_lib}/girepository-1.0"

# Python paths for Intel Mac
python_framework = "/Library/Frameworks/Python.framework/Versions/3.13"
python_site_packages = f"{python_framework}/lib/python3.13/site-packages"
python_lib = f"{python_framework}/lib/python3.13"

print(f"💻 Intel Mac build configuration:")
print(f"   Homebrew: {homebrew}")
print(f"   Python site-packages: {python_site_packages}")

# Keep list tight; expand if otool shows missing libs
gtk_libs_patterns = [
    "libadwaita-1.*.dylib",
    "libgtk-4.*.dylib",
    "libgdk-4.*.dylib",
    "libgdk_pixbuf-2.0.*.dylib",
    "libvte-2.91.*.dylib",
    "libvte-2.91-gtk4.*.dylib",
    "libgraphene-1.0.*.dylib",
    "libpango-1.*.dylib",
    "libpangocairo-1.*.dylib",
    "libharfbuzz.*.dylib",
    "libfribidi.*.dylib",
    "libcairo.*.dylib",
    "libcairo-gobject.*.dylib",
    "libgobject-2.0.*.dylib",
    "libglib-2.0.*.dylib",
    "libgio-2.0.*.dylib",
    "libgmodule-2.0.*.dylib",
    "libintl.*.dylib",
    "libffi.*.dylib",
    "libicu*.dylib",
]

binaries = []
for pat in gtk_libs_patterns:
    for src in glob.glob(os.path.join(hb_lib, pat)):
        # Special handling for VTE and Adwaita libraries to avoid nested Frameworks structure
        if "vte" in pat.lower() or "adwaita" in pat.lower():
            binaries.append((src, "."))  # Place directly in Frameworks root
        else:
            binaries.append((src, "Frameworks"))

# GI typelibs
datas = []
for typelib in glob.glob(os.path.join(hb_gir, "*.typelib")):
    datas.append((typelib, "girepository-1.0"))

# Shared data: schemas, icons, gtk-4.0 assets
datas += [
    (os.path.join(hb_share, "glib-2.0", "schemas"), "Resources/share/glib-2.0/schemas"),
    (os.path.join(hb_share, "icons", "Adwaita"),    "Resources/share/icons/Adwaita"),
    (os.path.join(hb_share, "gtk-4.0"),               "Resources/share/gtk-4.0"),
    ("sshpilot", "sshpilot"),
    ("sshpilot/resources/sshpilot.gresource", "Resources/sshpilot"),
]

# Optional helper binaries
sshpass = f"{homebrew}/bin/sshpass"
if os.path.exists(sshpass):
    binaries.append((sshpass, "Resources/bin"))

# Python dependencies from system Python
python_deps = [
    "paramiko",
    "cryptography", 
    "keyring",
    "psutil",
    "cairo",
    "gi",
    "PyGObject",
]

# Add Python packages to datas (let PyInstaller handle gi automatically)
for dep in python_deps:
    if dep == "gi":
        # Skip gi - let PyInstaller's built-in hooks handle it
        continue
    dep_path = os.path.join(python_site_packages, dep)
    if os.path.exists(dep_path):
        # Regular package handling
        datas.append((dep_path, dep))

# Add Python package metadata
for dep in python_deps:
    dist_info = f"{dep}-*.dist-info"
    for dist_path in glob.glob(os.path.join(python_site_packages, dist_info)):
        datas.append((dist_path, os.path.basename(dist_path)))

# Comprehensive hidden imports
hiddenimports = collect_submodules("gi")
hiddenimports += [
    "gi._gi_cairo", 
    "gi.repository.cairo", 
    "cairo",
    "paramiko",
    "paramiko.client",
    "paramiko.ssh_exception",
    "cryptography",
    "cryptography.fernet",
    "cryptography.hazmat",
    "keyring",
    "keyring.backends",
    "psutil",
    "psutil._psutil_osx",
    "psutil._psutil_posix",
]

# Add all submodules for each dependency
for dep in python_deps:
    try:
        hiddenimports += collect_submodules(dep)
    except:
        pass

block_cipher = None

a = Analysis(
    [entry_py],
    pathex=[python_site_packages, python_lib],  # FIXED: Include Python paths
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=["."],
    runtime_hooks=["hook-gtk_runtime.py"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    icon=icon_file if os.path.exists(icon_file) else None,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=app_name,
)

app = BUNDLE(
    coll,
    name=f"{app_name}.app",
    icon=icon_file if os.path.exists(icon_file) else None,
    bundle_identifier="app.sshpilot",
    info_plist={
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
