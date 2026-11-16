# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
project('gtkghostty', 'c',
  version: '0.3.1',
  license: 'LGPL-2.1-or-later',
  license_files: ['COPYING'],
  meson_version: '>= 1.0',
  default_options: ['c_std=c11', 'warning_level=2', 'buildtype=debugoptimized'],
)

gnome = import('gnome')

gtk = dependency('gtk4', version: '>= 4.14')
libghostty_vt = dependency('libghostty-vt', version: '>= 1.0.0')

cc = meson.get_compiler('c')
add_project_arguments(
  cc.get_supported_arguments('-Wno-unused-parameter', '-Wno-missing-field-initializers'),
  language: 'c',
)

subdir('src')
