project('sshpilot',
  version: '5.5.9',  # keep in sync with src/sshpilot/__init__.py (scripts/bump-version.sh bumps both)
  license: 'GPL-3.0-or-later',
  meson_version: '>= 0.59.0',
)

gnome = import('gnome')
python = import('python')
i18n = import('i18n')

py = python.find_installation('python3')

app_id = 'io.github.mfat.sshpilot'

prefix = get_option('prefix')
datadir = prefix / get_option('datadir')
bindir = prefix / get_option('bindir')
localedir = prefix / get_option('localedir')
pkgdatadir = datadir / app_id

# Where the Python package lands. The default -- the interpreter's site-packages
# -- is right for anything built once per Python version (deb, Arch, Flatpak).
# A single noarch RPM installed across several distros cannot use it: the path
# carries the Python minor version, so the payload would be invisible to any
# other interpreter. Such builds pass a version-independent directory instead;
# the launcher and build_config.py both take the path from here, so nothing
# needs to guess it.
_python_install_dir = get_option('python_install_dir')
if _python_install_dir == ''
  moduledir = py.get_install_dir()
elif _python_install_dir.startswith('/')
  moduledir = _python_install_dir
else
  moduledir = prefix / _python_install_dir
endif

subdir('data')
subdir('src')
subdir('po')

# Standalone agent launcher (not generated) shipped alongside the app.
install_data('sshpilot-agent',
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)

gnome.post_install(
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
