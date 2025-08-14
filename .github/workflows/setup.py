from setuptools import setup

APP = ['run.py']
OPTIONS = {
    'argv_emulation': True,
    'packages': ['gi', 'paramiko', 'cryptography', 'secretstorage', 'matplotlib'],
    'includes': ['gi.repository.Gtk', 'gi.repository.Adw', 'gi.repository.Vte'],
    'plist': {'CFBundleDevelopmentRegion': 'English'},
}

setup(
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)