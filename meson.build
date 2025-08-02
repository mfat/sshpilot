project('sshPilot', 'c',
  version: '1.0.0',
  license: 'GPL-3.0',
  meson_version: '>= 0.59.0',
  default_options: [
    'warning_level=2',
    'werror=false',
  ],
)

# Project information
project_id = 'io.github.mfat.sshpilot'
project_name = 'sshPilot'

# Dependencies
python = import('python')
py_installation = python.find_installation('python3')

gnome = import('gnome')
i18n = import('i18n')

# Required dependencies
dependency('python3', version: '>=3.10')
dependency('gobject-introspection-1.0', version: '>=1.72')
dependency('gtk4', version: '>=4.6')
dependency('libadwaita-1', version: '>=1.2')
dependency('vte-2.91-gtk4', version: '>=0.70')

# Python dependencies (checked at runtime)
python_deps = [
  'gi',
  'paramiko',
  'yaml',
  'secretstorage',
  'cryptography',
  'matplotlib',
]

# Check Python dependencies
foreach dep : python_deps
  if not py_installation.found()
    error('Python 3 installation not found')
  endif
endforeach

# Directories
prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
datadir = prefix / get_option('datadir')
pkgdatadir = datadir / project_id
pythondir = py_installation.get_install_dir()

# Configuration data
conf = configuration_data()
conf.set('PYTHON', py_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', prefix / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)
conf.set('project_id', project_id)

# Install Python package
install_subdir('src/io.github.mfat.sshpilot',
  install_dir: pythondir,
  strip_directory: false
)

# Install data files
install_data('data/io.github.mfat.sshpilot.desktop',
  install_dir: datadir / 'applications'
)

install_data('data/io.github.mfat.sshpilot.appdata.xml',
  install_dir: datadir / 'metainfo'
)

# Install icon
install_data('src/io.github.mfat.sshpilot/resources/sshpilot.png',
  install_dir: datadir / 'icons' / 'hicolor' / '256x256' / 'apps',
  rename: project_id + '.png'
)

# Install GSchema
install_data('data/io.github.mfat.sshpilot.gschema.xml',
  install_dir: datadir / 'glib-2.0' / 'schemas'
)

# Create and install launcher script
launcher_conf = configuration_data()
launcher_conf.set('PYTHON', py_installation.full_path())
launcher_conf.set('pkgdatadir', pkgdatadir)
launcher_conf.set('project_id', project_id)

launcher = configure_file(
  input: 'data/sshpilot.in',
  output: 'sshpilot',
  configuration: launcher_conf,
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x'
)

# Compile GLib resources
gnome.compile_resources(project_id,
  'src/io.github.mfat.sshpilot/resources/sshpilot.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

# Post-install script
gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)

# Summary
summary({
  'prefix': prefix,
  'bindir': bindir,
  'datadir': datadir,
  'pythondir': pythondir,
}, section: 'Directories')

summary({
  'Python': py_installation.full_path(),
  'Version': meson.project_version(),
}, section: 'Configuration')