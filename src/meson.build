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
conf.set('MODULEDIR', moduledir)

configure_file(
  input: 'build_config.py.in',
  output: 'build_config.py',
  configuration: conf,
  install: true,
  install_dir: moduledir / 'sshpilot',
)

# Install the Python package. The UI sources live only in the gresource, so drop
# resources/ui; example plugins are developer references (users get them from the
# plugin registry), matching the setuptools exclude in pyproject.toml.
install_subdir('sshpilot',
  install_dir: moduledir,
  # 'locale' holds the catalogues compiled by scripts/build_gresource.sh for
  # runs that never see Meson (the checkout, pip). Meson builds and installs
  # its own from po/ into localedir, so shipping these too would be a second,
  # staler copy of the same thing.
  exclude_directories: ['resources/ui', 'plugins/examples', 'locale'],
  # The committed bundle is the fallback main.py uses for pip/checkout runs that
  # never see Meson. Meson compiles and installs its own into pkgdatadir, which
  # main.py prefers, so shipping this one too is 3.4 MB of the same bytes twice
  # -- rpmlint fails the build on it (files-duplicated-waste).
  exclude_files: ['resources/sshpilot.gresource'],
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
