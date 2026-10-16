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

# GTK hands its window icon to Wayland compositors (xdg-toplevel-icon) as
# pixels, never as a name, and renders every size the theme reports as
# scalable at a fixed 48px. KWin then uses that one buffer in place of the
# .desktop icon, so a scalable-only icon is blurry in Plasma's large Alt-Tab
# switcher (GNOME/gtk#7845, closed upstream without a fix). hicolor's
# 256x256 and 512x512 directories are Type=Scalable, so they hit the same
# path; 128x128 and 192x192 are the largest Threshold sizes, which GTK
# renders at their own size and sends alongside the 48px buffer.
foreach size : ['128x128', '192x192']
  install_data(app_icon,
    install_dir: datadir / 'icons' / 'hicolor' / size / 'apps',
  )
endforeach

# Man pages for both binaries. Installed here rather than by a single distro's
# packaging, so every package (deb, rpm, Arch, Flatpak) ships them.
install_man(
  'sshpilot.1',
  'sshpilot-agent.1',
)
