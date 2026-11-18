# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
api_version = '1'

# A private install (see meson_options.txt) ships only what an application
# loads at run time, out of the shared library paths.
private_libdir = get_option('private_libdir')
private_install = private_libdir != ''
lib_install_dir = get_option('libdir') / private_libdir
typelib_install_dir = lib_install_dir / 'girepository-1.0'

public_headers = files('gtkghostty.h', 'gtk-ghostty-terminal.h', 'gtk-ghostty-pty.h', 'gtk-ghostty-visibility.h')

sources = files(
  'gtk-ghostty-terminal.c',
  'gtk-ghostty-pty.c',
  'gtk-ghostty-tables.c',
)

libgtkghostty = shared_library('gtkghostty-' + api_version,
  sources,
  dependencies: [gtk, libghostty_vt],
  c_args: ['-DG_LOG_DOMAIN="GtkGhostty"', '-DGTK_GHOSTTY_COMPILATION',
           '-DGTK_GHOSTTY_VERSION="@0@"'.format(meson.project_version())],
  gnu_symbol_visibility: 'hidden',
  install: true,
  # libgtkghostty-1.so.0 on Linux and macOS; on Windows a soversion would
  # rename the DLL (gtkghostty-1-0.dll) that applications bundle by name.
  # install_dir only for a private install: by default Meson puts a Windows
  # DLL in bin, next to the programs that load it.
  kwargs: (host_machine.system() == 'windows' ? {} : {'soversion': '0'})
          + (private_install ? {'install_dir': lib_install_dir} : {}),
)

if not private_install
  install_headers(public_headers, subdir: 'gtkghostty-' + api_version)
endif

gtkghostty_dep = declare_dependency(
  link_with: libgtkghostty,
  include_directories: include_directories('.'),
  dependencies: [gtk],
)

if get_option('introspection')
  gnome.generate_gir(libgtkghostty,
    sources: public_headers + files('gtk-ghostty-terminal.c', 'gtk-ghostty-pty.c'),
    namespace: 'GtkGhostty',
    nsversion: api_version,
    identifier_prefix: 'GtkGhostty',
    symbol_prefix: 'gtk_ghostty',
    includes: ['Gtk-4.0'],
    header: 'gtkghostty.h',
    install: true,
    install_dir_gir: private_install ? false : get_option('datadir') / 'gir-1.0',
    install_dir_typelib: typelib_install_dir,
  )
endif

if not private_install
  import('pkgconfig').generate(libgtkghostty,
  name: 'gtkghostty',
  description: 'GTK4 terminal widget over libghostty-vt',
  subdirs: 'gtkghostty-' + api_version,
  requires: ['gtk4'],
  requires_private: ['libghostty-vt'],
  )
endif
