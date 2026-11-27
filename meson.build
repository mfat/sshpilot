project('sshpilot',
  version: '6.5.2',  # keep in sync with src/sshpilot/__init__.py (scripts/bump-version.sh bumps both)
  license: 'GPL-3.0-or-later',
  meson_version: '>= 0.59.0',
)

gnome = import('gnome')
fs = import('fs')
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

# RDP connections in a tab: gtk4-frdp, built from the copy in
# subprojects/gtk4-frdp (scripts/update-gtk4-frdp.sh) and installed privately
# into <libdir>/sshpilot, where rdp_tab.py looks for it. Without it RDP opens
# in FreeRDP's own window. Private so the package never clashes with a system
# gtk4-frdp and needs no separate package.
rdp_tab_libdir = ''
rdp_tab_deps_found = true
foreach dep : [['freerdp3', '>= 3.11.0'], ['freerdp-client3', '>= 3.11.0'],
               ['winpr3', '>= 3.11.0'], ['gtk4', '>= 4.14'],
               ['gobject-introspection-1.0', '>= 1.66']]
  if rdp_tab_deps_found
    rdp_tab_deps_found = dependency(dep[0], version: dep[1],
      required: get_option('rdp_tab')).found()
  endif
endforeach
if rdp_tab_deps_found and not get_option('rdp_tab').disabled()
  subproject('gtk4-frdp', default_options: [
    'private_libdir=sshpilot',
    'introspection=true',
    'tests=false',
    'examples=false',
    'documentation=false',
  ])
  rdp_tab_libdir = prefix / get_option('libdir') / 'sshpilot'
endif

# The gtkghostty terminal widget (an opt-in backend), built from the copy in
# subprojects/gtkghostty (scripts/update-gtkghostty.sh) and installed privately
# into <libdir>/sshpilot like gtk4-frdp. It needs libghostty-vt, which
# distributions do not package yet: builds that have it (CI-built deb/rpm,
# Arch) build the widget, the others (Launchpad, COPR) leave it out and the
# backend is simply not offered.
gtkghostty_libdir = ''
gtkghostty_deps_found = true
foreach dep : [['libghostty-vt', '>= 1.0.0'], ['gtk4', '>= 4.14'],
               ['gobject-introspection-1.0', '>= 1.66']]
  if gtkghostty_deps_found
    gtkghostty_deps_found = dependency(dep[0], version: dep[1],
      required: get_option('gtkghostty')).found()
  endif
endforeach
if gtkghostty_deps_found and not get_option('gtkghostty').disabled()
  subproject('gtkghostty', default_options: [
    'private_libdir=sshpilot',
    'introspection=true',
  ])
  gtkghostty_libdir = prefix / get_option('libdir') / 'sshpilot'
  if get_option('bundle_libghostty_vt') and host_machine.system() == 'linux'
    # Under its soname, so the widget's DT_NEEDED resolves to this copy once
    # terminal_backend_gtkghostty.py has loaded it from here.
    ghostty_vt = dependency('libghostty-vt')
    install_data(ghostty_vt.get_variable(pkgconfig: 'libdir') / 'libghostty-vt.so.' + ghostty_vt.version(),
      rename: 'libghostty-vt.so.1',
      install_dir: get_option('libdir') / 'sshpilot',
    )
    # Its MIT licence and third-party manifest travel with the copy, which
    # packaging/windows/libghostty-vt/build.py --prefix puts in the prefix.
    ghostty_vt_licences = ghostty_vt.get_variable(pkgconfig: 'prefix') / 'share' / 'licenses' / 'libghostty-vt'
    foreach licence : ['LICENSE', 'third-party.json']
      if fs.is_file(ghostty_vt_licences / licence)
        install_data(ghostty_vt_licences / licence,
          install_dir: get_option('libdir') / 'sshpilot' / 'licenses' / 'libghostty-vt')
      endif
    endforeach
  endif
endif

subdir('data')
subdir('src')
subdir('po')

# Standalone agent and daemon launchers (not generated) shipped alongside the app.
install_data('sshpilot-agent',
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)
install_data('sshpilot-daemon',
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)

# Optional systemd user unit. Distro packagers may symlink or copy this into
# /usr/lib/systemd/user/; upstream installs the reference unit under share/.
install_data('packaging/systemd/sshpilot-daemon.service',
  install_dir: datadir / 'sshpilot' / 'systemd',
)

summary({'RDP tab (bundled gtk4-frdp)': rdp_tab_libdir != '' ? rdp_tab_libdir : 'no',
         'gtkghostty terminal (bundled)': gtkghostty_libdir != '' ? gtkghostty_libdir : 'no'},
  section: 'Features')

gnome.post_install(
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
