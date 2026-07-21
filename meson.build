project('sshpilot',
  version: '5.6.5',  # keep in sync with src/sshpilot/__init__.py (scripts/bump-version.sh bumps both)
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

# Where the Python package lands: the interpreter's site-packages, which is
# right for anything built once per Python version (deb, Arch, Flatpak).
#
# A single noarch RPM installed across several distros cannot use that, because
# the path carries the Python minor version and the payload would be invisible
# to every other interpreter. Such builds override it with Meson's built-in
# `-Dpython.purelibdir=...` (see packaging/fedora/rpm.spec) rather than an
# option defined here: a project-defined option only exists in tarballs new
# enough to carry it, and the spec is applied to release tarballs that predate
# it. The launcher and build_config.py both read the path from here either way.
moduledir = py.get_install_dir()

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
