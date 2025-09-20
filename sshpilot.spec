# sshpilot.spec — build with: pyinstaller --clean sshpilot.spec
import os, glob, platform
from PyInstaller.utils.hooks import collect_submodules

app_name = "SSHPilot"
entry_py = "run.py"
icon_file = "packaging/macos/sshpilot.icns"

# Detect architecture and set Homebrew path
arch = platform.machine()
if arch == "arm64":
    # Apple Silicon Mac
    homebrew = "/opt/homebrew"
    print(f"🍎 Detected Apple Silicon Mac (ARM64), using Homebrew at: {homebrew}")
else:
    # Intel Mac
    homebrew = "/usr/local/"
    print(f"💻 Detected Intel Mac (x86_64), using Homebrew at: {homebrew}")

hb_lib = f"{homebrew}/lib"
hb_share = f"{homebrew}/share"
hb_gir = f"{hb_lib}/girepository-1.0"

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
    ("sshpilot/io.github.mfat.sshpilot.svg", "share/icons"),
]

# Add libadwaita locale files if they exist
libadwaita_locale = "/opt/homebrew/Cellar/libadwaita/1.7.6/share/locale"
if os.path.exists(libadwaita_locale):
    datas.append((libadwaita_locale, "Resources/share/locale"))
    print(f"Added libadwaita locale files: {libadwaita_locale}")

# Add GDK-Pixbuf loaders and cache
gdkpixbuf_loaders = f"{homebrew}/lib/gdk-pixbuf-2.0/2.10.0"
if os.path.exists(gdkpixbuf_loaders):
    datas.append((gdkpixbuf_loaders, "Resources/lib/gdk-pixbuf-2.0/2.10.0"))
    print(f"Added GDK-Pixbuf loaders: {gdkpixbuf_loaders}")

# Add keyring package files explicitly
keyring_package = f"{homebrew}/lib/python3.13/site-packages/keyring"
if os.path.exists(keyring_package):
    datas.append((keyring_package, "keyring"))
    print(f"Added keyring package: {keyring_package}")


# Optional helper binaries
sshpass = f"{homebrew}/bin/sshpass"
if os.path.exists(sshpass):
    binaries.append((sshpass, "Resources/bin"))

# Cairo Python bindings (required for Cairo Context)
cairo_gi_binding = f"{homebrew}/lib/python3.13/site-packages/gi/_gi_cairo.cpython-313-darwin.so"
if os.path.exists(cairo_gi_binding):
    binaries.append((cairo_gi_binding, "gi"))

hiddenimports = collect_submodules("gi")
hiddenimports += ["gi._gi_cairo", "gi.repository.cairo", "cairo"]
# Add keyring for askpass functionality
hiddenimports += ["keyring"]
# Add all keyring backends
hiddenimports += ["keyring.backends", "keyring.backends.macOS", "keyring.backends.libsecret", "keyring.backends.SecretService"]

block_cipher = None

a = Analysis(
    [entry_py],
    pathex=[],
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
