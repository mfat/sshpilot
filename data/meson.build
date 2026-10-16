# Desktop entry and AppStream metainfo are translatable templates merged with
# the catalogs in po/ (identity merge until translations land). Both are
# validated as part of `meson test`.

po_dir = meson.project_source_root() / 'po'

desktop_file = i18n.merge_file(
  input: 'io.github.mfat.sshpilot.desktop.in',
  output: 'io.github.mfat.sshpilot.desktop',
  type: 'desktop',
  po_dir: po_dir,
  install: true,
  install_dir: datadir / 'applications',
)

desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test('validate-desktop', desktop_file_validate,
    args: [desktop_file],
  )
endif

metainfo_file = i18n.merge_file(
  input: 'io.github.mfat.sshpilot.metainfo.xml.in',
  output: 'io.github.mfat.sshpilot.metainfo.xml',
  type: 'xml',
  po_dir: po_dir,
  install: true,
  install_dir: datadir / 'metainfo',
)

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('validate-metainfo', appstreamcli,
    args: ['validate', '--no-net', '--explain', metainfo_file],
  )
endif

app_icon = 'icons/hicolor/scalable/apps/io.github.mfat.sshpilot.svg'

install_data(app_icon,
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'apps',
)

# GTK rasterizes scalable-only window icons at 48px before sending them to a
# Wayland compositor. Advertise the same vector artwork as a fixed 256px icon
# too, so large Plasma task switchers receive a suitably sized texture.
install_data(app_icon,
  install_dir: datadir / 'icons' / 'hicolor' / '256x256' / 'apps',
)

# Man pages for both binaries. Installed here rather than by a single distro's
# packaging, so every package (deb, rpm, Arch, Flatpak) ships them.
install_man(
  'sshpilot.1',
  'sshpilot-agent.1',
)
