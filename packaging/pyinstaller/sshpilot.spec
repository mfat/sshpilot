# sshpilot.spec — build with: pyinstaller --clean sshpilot.spec
import os, sys, glob, platform, sysconfig
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs, copy_metadata

# Resolve the current Python site-packages directory dynamically
site_packages_dir = Path(sysconfig.get_path("platlib"))

# This spec lives in packaging/pyinstaller/; anchor paths to the repo root.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir, os.pardir))

# Local helpers for GI dylib placement (must live next to this spec).
sys.path.insert(0, SPECPATH)
from gtk_bundle import collect_homebrew_dylibs  # noqa: E402

app_name = "SSHPilot"
entry_py = os.path.join(ROOT, "run.py")
icon_file = os.path.join(ROOT, "packaging", "macos", "sshpilot.icns")

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
    "libgtksourceview-5.*.dylib",
    "libwebkitgtk-6.*.dylib",
    "libjavascriptcoregtk-6.*.dylib",
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

# Place GI dylibs at Contents/Frameworks/ (dest "."). Nested Frameworks/Frameworks
# breaks typelib dlopen of bare sonames such as libgtksourceview-5.0.dylib.
# The stock hook-gi.repository.GtkSource (pyinstaller#3893) also uses dest ".";
# this Homebrew glob is belt-and-suspenders for libs GI hooks may miss.
binaries = collect_homebrew_dylibs(hb_lib, gtk_libs_patterns)

# GI typelibs
datas = []
for typelib in glob.glob(os.path.join(hb_gir, "*.typelib")):
    datas.append((typelib, "girepository-1.0"))

# Shared data: schemas, icons, gtk-4.0 assets
datas += [
    (os.path.join(hb_share, "glib-2.0", "schemas"), "Resources/share/glib-2.0/schemas"),
    (os.path.join(hb_share, "icons", "Adwaita"),    "Resources/share/icons/Adwaita"),
    (os.path.join(hb_share, "gtk-4.0"),               "Resources/share/gtk-4.0"),
    (os.path.join(ROOT, "src", "sshpilot"), "sshpilot"),
    (os.path.join(ROOT, "src", "sshpilot", "resources", "sshpilot.gresource"), "Resources/sshpilot"),
    (os.path.join(ROOT, "data", "icons", "hicolor", "scalable", "apps",
                  "io.github.mfat.sshpilot.svg"), "share/icons"),
]

# Find libadwaita share data in Cellar if not in standard share location
libadwaita_share_standard = os.path.join(hb_share, "libadwaita-1")
libadwaita_share_cellar = None

if os.path.exists(libadwaita_share_standard):
    datas.append((libadwaita_share_standard, "Resources/share/libadwaita-1"))
    print(f"Found libadwaita share data at: {libadwaita_share_standard}")
else:
    # Look for libadwaita in Cellar
    cellar_libadwaita = f"{homebrew}/Cellar/libadwaita"
    if os.path.exists(cellar_libadwaita):
        for version_dir in os.listdir(cellar_libadwaita):
            version_path = os.path.join(cellar_libadwaita, version_dir, "share", "libadwaita-1")
            if os.path.exists(version_path):
                libadwaita_share_cellar = version_path
                datas.append((libadwaita_share_cellar, "Resources/share/libadwaita-1"))
                print(f"Found libadwaita share data in Cellar at: {libadwaita_share_cellar}")
                break
    
    if not libadwaita_share_cellar:
        print(f"WARNING: Could not find libadwaita share data at {libadwaita_share_standard} or in Cellar")

# Add libadwaita locale files if they exist
libadwaita_locale_cellar = f"{homebrew}/Cellar/libadwaita"
if os.path.exists(libadwaita_locale_cellar):
    for version_dir in os.listdir(libadwaita_locale_cellar):
        locale_path = os.path.join(libadwaita_locale_cellar, version_dir, "share", "locale")
        if os.path.exists(locale_path):
            datas.append((locale_path, "Resources/share/locale"))
            print(f"Added libadwaita locale files: {locale_path}")
            break

# Find gtksourceview5 share data in standard location or Cellar
gtksourceview5_share_standard = os.path.join(hb_share, "gtksourceview-5")
gtksourceview5_share_cellar = None

if os.path.exists(gtksourceview5_share_standard):
    datas.append((gtksourceview5_share_standard, "Resources/share/gtksourceview-5"))
    print(f"Found gtksourceview5 share data at: {gtksourceview5_share_standard}")
else:
    # Look for gtksourceview5 in Cellar (package name might be gtksourceview5 or gtksourceview-5)
    for cellar_name in ["gtksourceview5", "gtksourceview-5"]:
        cellar_gtksourceview5 = f"{homebrew}/Cellar/{cellar_name}"
        if os.path.exists(cellar_gtksourceview5):
            for version_dir in os.listdir(cellar_gtksourceview5):
                version_path = os.path.join(cellar_gtksourceview5, version_dir, "share", "gtksourceview-5")
                if os.path.exists(version_path):
                    gtksourceview5_share_cellar = version_path
                    datas.append((gtksourceview5_share_cellar, "Resources/share/gtksourceview-5"))
                    print(f"Found gtksourceview5 share data in Cellar at: {gtksourceview5_share_cellar}")
                    break
            if gtksourceview5_share_cellar:
                break
    
    if not gtksourceview5_share_cellar:
        print(f"WARNING: Could not find gtksourceview5 share data at {gtksourceview5_share_standard} or in Cellar")

# Add GDK-Pixbuf loaders and cache
gdkpixbuf_loaders = f"{homebrew}/lib/gdk-pixbuf-2.0/2.10.0"
if os.path.exists(gdkpixbuf_loaders):
    datas.append((gdkpixbuf_loaders, "Resources/lib/gdk-pixbuf-2.0/2.10.0"))
    print(f"Added GDK-Pixbuf loaders: {gdkpixbuf_loaders}")

# Add keyring package files explicitly
keyring_package = site_packages_dir / "keyring"
if keyring_package.exists():
    datas.append((str(keyring_package), "keyring"))
    print(f"Added keyring package: {keyring_package}")
# Keyring metadata for entry-point backend discovery (hooks-contrib/keyring hook).
try:
    datas += copy_metadata("keyring")
except Exception as exc:
    print(f"WARNING: could not copy keyring metadata: {exc}")


# Cairo Python bindings (required for Cairo Context)
gi_site_packages = site_packages_dir / "gi"
if gi_site_packages.exists():
    cairo_gi_binding = next((p for p in gi_site_packages.glob("_gi_cairo.*")), None)
    if cairo_gi_binding and cairo_gi_binding.exists():
        binaries.append((str(cairo_gi_binding), "gi"))


hiddenimports = collect_submodules("gi")
hiddenimports += ["gi._gi_cairo", "gi.repository.cairo", "cairo"]
# Force the stock PyInstaller GI hooks to run (hook-gi.repository.GtkSource
# from pyinstaller#3893, etc.). GtkSource is imported behind try/except in app
# code, so analysis may miss it without an explicit hiddenimport. The hook
# collects the shared library at Frameworks root (dest ".") and on macOS
# rewrites the typelib to @loader_path/… — but only for the configured version.
hiddenimports += [
    "gi.repository.Gtk",
    "gi.repository.Gdk",
    "gi.repository.GtkSource",
    "gi.repository.Adw",
    "gi.repository.Vte",
]
# Built-in plugins are imported dynamically (the loader scans the dir), so
# PyInstaller can't see them by following imports — collect them explicitly,
# and bundle their plugin.json manifests (read from disk at runtime).
hiddenimports += collect_submodules("sshpilot.plugins.builtin")
# Built-in manifests only — example plugins are dev references, never shipped.
datas += collect_data_files("sshpilot.plugins.builtin", includes=["**/plugin.json"])
# Add keyring for askpass functionality
hiddenimports += ["keyring"]
# Add all keyring backends
hiddenimports += ["keyring.backends", "keyring.backends.macOS", "keyring.backends.libsecret", "keyring.backends.SecretService"]
# certifi / cryptography: listed so the (hooks-contrib) hooks fire — hook-certifi
# collects cacert.pem for the HTTPS update check; hook-cryptography collects
# backends + OpenSSL 3 modules. Both packages must be installed in the build env.
hiddenimports += ["certifi", "cryptography"]

# KeePass (.kdbx) secret backend: pykeepass + its (partly compiled) deps. The import is
# lazy/optional, so PyInstaller's import-following may miss it — collect explicitly so the
# self-contained bundle can offer the backend. Wrapped so a missing dep never breaks the build.
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

# Official GI hooks default to Gtk/GtkSource 3.x; sshPilot needs GTK4 + GtkSource 5
# (see https://github.com/pyinstaller/pyinstaller/pull/3893 and hooks-config docs).
gi_hooksconfig = {
    "gi": {
        "module-versions": {
            "Gtk": "4.0",
            "Gdk": "4.0",
            "GtkSource": "5",
        },
        # Keep the bundle lean — full icon/theme trees are huge; we already ship
        # Adwaita icons via datas above.
        "icons": ["Adwaita"],
        "themes": ["Adwaita"],
    },
}

block_cipher = None

a = Analysis(
    [entry_py],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[SPECPATH],
    hooksconfig=gi_hooksconfig,
    runtime_hooks=[os.path.join(SPECPATH, "hook-gtk_runtime.py")],
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
    bundle_identifier="io.github.mfat.sshpilot",
    info_plist={
        "NSHighResolutionCapable": True,
        # Matches the GTK stack actually inside the bundle, not the oldest macOS
        # PyInstaller could target: the Homebrew dylibs collected by
        # gtk_bundle.py carry `minos 14.0` (LC_BUILD_VERSION), so 12 and 13 hit
        # a dyld failure at launch. Declaring 14.0 turns that crash into the
        # Finder's "requires a newer version of macOS" dialog. Raise this
        # whenever the build runners move to a newer macOS.
        "LSMinimumSystemVersion": "14.0",
    },
)

# NOTE: no codesign here. The bundle is still modified after this point
# (pyinstaller.sh adds sshpass and the GtkSourceView ABI symlink), so signing
# now would be invalidated by those changes and produce the "app is damaged"
# Gatekeeper error. Ad-hoc signing happens once, last, in pyinstaller.sh.
