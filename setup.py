from setuptools import setup

APP = ['run.py']
OPTIONS = {
    'argv_emulation': True,
    'packages': ['gi', 'paramiko', 'cryptography', 'secretstorage', 'matplotlib'],
    'excludes': ['PIL', 'Pillow'],
    'recipe_plugins': [],  # Disable all recipe plugins to avoid PIL auto-inclusion
    'includes': [
        'gi.repository.Gtk', 'gi.repository.Adw', 'gi.repository.Vte',
        'gi.repository.GLib', 'gi.repository.GObject', 'gi.repository.Gdk',
        'gi.repository.cairo', 'cairo'
    ],
    'resources': [
        '/opt/homebrew/share/glib-2.0/schemas',
        '/opt/homebrew/share/icons',
        '/opt/homebrew/lib/girepository-1.0'
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