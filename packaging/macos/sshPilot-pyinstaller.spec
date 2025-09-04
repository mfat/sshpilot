# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for building sshPilot macOS app bundle.

This spec ensures that the resulting .app bundle is fully self-contained:
- Includes Python runtime and all Python packages from requirements.txt
- Bundles GTK, libadwaita and related Homebrew libraries
- Copies application resources and icons
"""

import os
from PyInstaller.utils.hooks import collect_submodules

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
icon_file = os.path.join(os.path.dirname(__file__), 'sshPilot.icns')


datas = [
    (os.path.join(project_root, 'sshpilot', 'resources'), 'sshpilot/resources'),
]

hiddenimports = [
    'gi',
    'gi.repository.Gtk',
    'gi.repository.Adw',
    'gi.repository.Gio',
    'gi.repository.GLib',
    'gi.repository.GObject',
    'gi.repository.Gdk',
    'gi.repository.Pango',
    'gi.repository.PangoFT2',
    'gi.repository.Vte',
    'paramiko',
    'cryptography',
    'keyring',
    'keyring.backends.secretstorage',
    'keyring.backends.macOS',
    'keyring.backends.kwallet',
    'keyring.backends.file',
    'nacl',
    'bcrypt',
    'psutil',
] + collect_submodules('keyring.backends')


a = Analysis(
    ['run.py'],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[project_root],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
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
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='sshPilot',
)
app = BUNDLE(
    coll,
    name='sshPilot.app',
    icon=icon_file,
    bundle_identifier='io.github.mfat.sshpilot',
    info_plist={
        'CFBundleName': 'sshPilot',
        'CFBundleDisplayName': 'sshPilot',
        'CFBundleVersion': '2.7.1',
        'CFBundleShortVersionString': '2.7.1',
        'LSMinimumSystemVersion': '10.15',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
    },
)
