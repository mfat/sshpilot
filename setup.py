from setuptools import setup

APP = ['run.py']
OPTIONS = {
    'argv_emulation': True,
    'packages': ['gi', 'paramiko', 'cryptography', 'secretstorage', 'matplotlib'],
    'includes': [
        'gi.repository.Gtk', 'gi.repository.Adw', 'gi.repository.Vte',
        'gi.repository.GLib', 'gi.repository.GObject', 'gi.repository.Gdk',
        'gi.repository.cairo', 'cairo'
    ],
    'resources': [
        '/opt/homebrew/share/glib-2.0/schemas',
        '/opt/homebrew/share/icons',
        '/opt/homebrew/lib/girepository-1.0',
        '/opt/homebrew/share/adwaita-icon-theme'
    ],
    'frameworks': [
        '/opt/homebrew/opt/gtk4/lib/libgtk-4.1.dylib',
        '/opt/homebrew/opt/glib/lib/libglib-2.0.0.dylib',
        '/opt/homebrew/opt/vte3/lib/libvte-2.91-gtk4.0.dylib',
        '/opt/homebrew/opt/icu4c/lib/libicudata.75.dylib',
        '/opt/homebrew/opt/icu4c/lib/libicui18n.75.dylib',
        '/opt/homebrew/opt/icu4c/lib/libicuio.75.dylib',
        '/opt/homebrew/opt/icu4c/lib/libicutu.75.dylib',
        '/opt/homebrew/opt/icu4c/lib/libicuuc.75.dylib',
        '/opt/homebrew/opt/graphene/lib/libgraphene-1.0.0.dylib'
    ],
    'plist': {
        'CFBundleDevelopmentRegion': 'English',
        'CFBundleIdentifier': 'com.sshpilot.app',
        'NSHumanReadableCopyright': 'Copyright 2025 sshpilot contributors'
    }
}

setup(
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app']
)