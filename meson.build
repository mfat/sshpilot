project('sshpilot',
  version: '5.5.7',  # keep in sync with src/sshpilot/__init__.py (scripts/release.sh bumps both)
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
moduledir = py.get_install_dir()

subdir('data')
subdir('src')
subdir('po')

gnome.post_install(
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
