# -*- mode: python ; coding: utf-8 -*-

import os

# Detect Homebrew prefix
homebrew = "/usr/local" if os.path.exists("/usr/local/Cellar") else "/opt/homebrew"

# Target Resources dir inside the macOS bundle that PyInstaller creates
res = "sshPilot.app/Contents/Resources"

datas = []
binaries = []

def add_tree(src: str, dst_rel: str):
    if not os.path.isdir(src):
        return
    for root, _dirs, files in os.walk(src):
        for f in files:
            full_src = os.path.join(root, f)
            rel_dir = os.path.relpath(root, src)
            dst = os.path.join(res, dst_rel, rel_dir)
            datas.append((full_src, dst))

# GI typelibs and GIRs
add_tree(os.path.join(homebrew, "lib", "girepository-1.0"), "lib/girepository-1.0")
add_tree(os.path.join(homebrew, "share", "gir-1.0"), "share/gir-1.0")

# gdk-pixbuf loaders and cache
add_tree(os.path.join(homebrew, "lib", "gdk-pixbuf-2.0"), "lib/gdk-pixbuf-2.0")
loaders_cache = os.path.join(homebrew, "share", "gdk-pixbuf-2.0", "loaders.cache")
if os.path.isfile(loaders_cache):
    datas.append((loaders_cache, os.path.join(res, "share/gdk-pixbuf-2.0")))

# glib schemas (compiled post-build by PyInstaller hook)
add_tree(os.path.join(homebrew, "share", "glib-2.0", "schemas"), "share/glib-2.0/schemas")

# themes/icons/gtk data
add_tree(os.path.join(homebrew, "share", "icons"), "share/icons")
add_tree(os.path.join(homebrew, "share", "themes"), "share/themes")
add_tree(os.path.join(homebrew, "share", "gtk-4.0"), "share/gtk-4.0")

# App gresource (keep bundled so runtime lookup works in both dev and bundle)
app_gresource = os.path.join(os.path.abspath('.'), 'sshpilot', 'resources', 'sshpilot.gresource')
if os.path.isfile(app_gresource):
    datas.append((app_gresource, os.path.join(res, 'app/sshpilot/resources')))

a = Analysis(
    ['run.py'],
    pathex=[os.path.abspath('.')],
    binaries=binaries,
    datas=datas,
    hiddenimports=['gi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='sshPilot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='sshPilot',
)
app = BUNDLE(
    coll,
    name='sshPilot.app',
    icon=None,
    bundle_identifier='io.github.mfat.sshpilot',
)
