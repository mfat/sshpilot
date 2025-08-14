# -*- mode: python ; coding: utf-8 -*-
import os
import subprocess

# Get Homebrew prefix
try:
    brew_prefix = subprocess.check_output(['brew', '--prefix']).decode().strip()
except:
    brew_prefix = '/opt/homebrew'  # Default for Apple Silicon

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[
        # Include GTK and GObject libraries
        (f'{brew_prefix}/lib/libgtk-4.1.dylib', '.'),
        (f'{brew_prefix}/lib/libglib-2.0.0.dylib', '.'),
        (f'{brew_prefix}/lib/libgobject-2.0.0.dylib', '.'),
        (f'{brew_prefix}/lib/libgio-2.0.0.dylib', '.'),
        (f'{brew_prefix}/lib/libadwaita-1.0.dylib', '.'),
        (f'{brew_prefix}/lib/libvte-2.91-gtk4.0.dylib', '.'),
    ],
    datas=[
        # Include GTK schemas and icons
        (f'{brew_prefix}/share/glib-2.0/schemas', 'share/glib-2.0/schemas'),
        (f'{brew_prefix}/share/icons', 'share/icons'),
        (f'{brew_prefix}/lib/girepository-1.0', 'lib/girepository-1.0'),
    ],
    hiddenimports=[
        'gi',
        'gi.repository.Gtk',
        'gi.repository.Adw', 
        'gi.repository.Vte',
        'gi.repository.GLib',
        'gi.repository.GObject',
        'gi.repository.Gdk',
        'gi.repository.cairo',
        'cairo',
        'paramiko',
        'cryptography',
        'secretstorage',
        'matplotlib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'Pillow'],
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
    upx=False,  # Disable UPX compression for better compatibility
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,  # Enable macOS argv emulation
    target_arch='universal2',  # Build universal binary for both Intel and ARM
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,  # Disable UPX compression
    upx_exclude=[],
    name='sshPilot',
)
app = BUNDLE(
    coll,
    name='sshpilot.app',  # Use lowercase name for consistency
    icon=None,
    bundle_identifier='io.github.mfat.sshpilot',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
        'CFBundleDocumentTypes': [],
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.15.0',
        'NSRequiresAquaSystemAppearance': False,
    },
)
