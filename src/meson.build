blueprint_compiler = find_program('blueprint-compiler')

# Compile Blueprint sources -> .ui into a dedicated build/ui/ dir (kept out of
# the package tree so it never collides with the generated `sshpilot` launcher).
ui_srcdir = meson.current_source_dir() / 'sshpilot' / 'resources' / 'ui'
blueprints = custom_target('blueprints',
  input: files(
    'sshpilot/resources/ui/add_folder_dialog.blp',
    'sshpilot/resources/ui/authorized_key_entry_dialog.blp',
    'sshpilot/resources/ui/authorized_keys_window.blp',
    'sshpilot/resources/ui/command_edit_dialog.blp',
    'sshpilot/resources/ui/connection_dialog.blp',
    'sshpilot/resources/ui/file_manager_window.blp',
    'sshpilot/resources/ui/key_chooser_dialog.blp',
    'sshpilot/resources/ui/known_hosts_editor.blp',
    'sshpilot/resources/ui/log_viewer_window.blp',
    'sshpilot/resources/ui/monospace_font_dialog.blp',
    'sshpilot/resources/ui/mount_progress_dialog.blp',
    'sshpilot/resources/ui/placeholder_dialog.blp',
    'sshpilot/resources/ui/preferences_window.blp',
    'sshpilot/resources/ui/properties_dialog.blp',
    'sshpilot/resources/ui/scp_download_window.blp',
    'sshpilot/resources/ui/scp_transfer_dialog.blp',
    'sshpilot/resources/ui/sftp_connection_dialog.blp',
    'sshpilot/resources/ui/shortcut_capture_dialog.blp',
    'sshpilot/resources/ui/shortcut_editor_window.blp',
    'sshpilot/resources/ui/sshcopyid_window.blp',
    'sshpilot/resources/ui/text_editor_window.blp',
    'sshpilot/resources/ui/window.blp',
  ),
  output: 'ui',
  command: [blueprint_compiler, 'batch-compile', '@OUTPUT@', ui_srcdir, '@INPUT@'],
)

# Bundle the GResource. The gresource.xml references `ui/*.ui` (compiled to the
# build tree) plus static assets (icons, svg, language-specs) from the source
# tree, so search both.
gnome.compile_resources(app_id,
  'sshpilot/resources/sshpilot.gresource.xml',
  gresource_bundle: true,
  dependencies: blueprints,
  source_dir: [
    meson.current_build_dir(),
    meson.current_source_dir() / 'sshpilot' / 'resources',
  ],
  install: true,
  install_dir: pkgdatadir,
)

# Build-time configuration consumed by the app to locate the gresource and
# translations when installed via Meson (setuptools/editable runs fall back).
conf = configuration_data()
conf.set('PKGDATADIR', pkgdatadir)
conf.set('LOCALEDIR', localedir)
conf.set('VERSION', meson.project_version())
conf.set('PROFILE', get_option('profile'))
conf.set('APP_ID', app_id)

configure_file(
  input: 'build_config.py.in',
  output: 'build_config.py',
  configuration: conf,
  install: true,
  install_dir: moduledir / 'sshpilot',
)

# Install the Python package. The UI sources live only in the gresource, so drop
# resources/ui from the installed package.
install_subdir('sshpilot',
  install_dir: moduledir,
  exclude_directories: ['resources/ui'],
)

# Generated launcher installed as `sshpilot` in bindir.
configure_file(
  input: 'sshpilot.in',
  output: 'sshpilot',
  configuration: conf,
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)
