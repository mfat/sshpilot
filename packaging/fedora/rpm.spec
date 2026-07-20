Name:           sshpilot
Version:        %{?version}%{!?version:5.5.9}
Release:        1%{?dist}
Summary:        Manage your servers with ease

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/sshpilot
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz


BuildArch:      noarch
# 0.60 for the built-in python.purelibdir option used in %%build.
BuildRequires:  meson >= 0.60
BuildRequires:  ninja-build
BuildRequires:  python3-devel
# Compiles the .blp sources into the .ui files bundled in the GResource.
# blueprint-compiler resolves widget types from the GIR files, so the -devel
# packages for every namespace the .blp files 'using' are build-time deps.
BuildRequires:  blueprint-compiler
BuildRequires:  gtk4-devel
BuildRequires:  libadwaita-devel
# glib-compile-resources, for the GResource bundle.
BuildRequires:  glib2-devel
BuildRequires:  gettext
# Both validators are run by `meson test` during %%check.
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
# gnome.post_install() resolves these at configure time (icon cache + desktop
# database refresh); without them meson setup fails.
BuildRequires:  gtk-update-icon-cache


# Define _metainfodir for openSUSE compatibility (not defined by default on openSUSE)
%{!?_metainfodir:%global _metainfodir %{_datadir}/metainfo}

# No explicit `Requires: python3`: the guidelines call for depending on the
# interpreter through the generated /usr/bin/python3 dependency instead, and a
# manual one would duplicate it.
Requires:       python3-gobject
Requires:       gtk4 >= 4.6
# 1.5 for Adw.Dialog / Adw.AlertDialog, both used unguarded.
Requires:       libadwaita >= 1.5
Requires:       vte291-gtk4 >= 0.70
Requires:       gtksourceview5 >= 5.0
Requires:       python3-cryptography
# Secret storage. python3-keyring rather than python3-secretstorage: keyring is
# what the code imports (secret_storage.py), and it pulls in whichever backend
# the platform needs.
Requires:       python3-keyring
# port_utils.py and wol.py.
Requires:       python3-psutil
Requires:       libsecret
Requires:       sshpass
Requires:       openssh-askpass
# Not optional: the embedded PyXterm terminal backend renders through a WebKit
# WebView (xterm_shell.py builds the page, terminal_backends.py drives it over a
# script-message handler). Without this the backend cannot start at all.
Requires:       webkitgtk6.0
# Optional: KeePass (.kdbx) secret backend (degrades gracefully if absent).
Recommends:     python3-pykeepass
# For the built-in telnet protocol plugin (degrades gracefully if absent)
Recommends:     telnet
# Deliberately absent: python3-certifi is only consulted by builds with no
# system CA store (the macOS bundle); update_checker.py falls back to the stdlib
# default context, which is the correct one here. python3-flask and
# python3-flask-socketio went with the old pyxtermjs server, replaced by the
# in-process PTY bridge -- nothing imports them any more.

%description
SSH Pilot is a user-friendly SSH connection manager featuring built-in tabbed
terminal, remote file management, key transfer, port forwarding and more. It's
an alternative to Putty, Termius and Mobaxterm.

%prep
%autosetup -n %{name}-%{version}

# Meson installs the launcher, the Python package, the compiled GResource, the
# desktop entry and AppStream metainfo (merged from the .in templates), the icon
# and sshpilot-agent. Nothing is replayed by hand here.
%build
# Keep the payload out of %%{python3_sitelib}. That path carries the Python minor
# version (…/python3.14/site-packages), so a noarch RPM built once and installed
# on a distro with any other Python would put its files where no interpreter
# looks -- installing cleanly and then failing to import. The previous spec
# papered over this by filtering the python(abi) dependency, which removed the
# error without removing the breakage. Both the launcher and sshpilot-agent
# locate the package here.
#
# python.purelibdir is Meson's own option, deliberately not one declared by this
# project: COPR builds this spec against the release tarball named in Source0,
# which for any tag older than the change is a tree with no such option in it,
# and meson setup would abort with "Unknown option". A built-in works on every
# tarball, including ones that predate this comment.
%meson -Dpython.purelibdir=%{_datadir}/%{name}
%meson_build

# po/LINGUAS is still empty, so Meson installs nothing under %%{_datadir}/locale
# and the %%find_lang below produces an empty list. rpm treats an empty %%files
# manifest as fatal by default, which would fail the build over the *absence* of
# a problem. This macro is exactly that switch -- "Should empty %%files manifest
# file terminate a build?" -- and clearing it downgrades the error to a warning.
#
# Keeping %%find_lang rather than dropping it means the .mo files get packaged
# automatically the day the first translation lands, with no spec change to
# remember. The guard given up is narrow: every other entry in %%files is an
# explicit path that rpm still verifies, and the unpackaged-files check that
# caught the missing man pages is untouched.
#
# Set here rather than in %%install so it is parsed before %%files reads it.
%global _empty_manifest_terminate_build 0

%install
%meson_install

# Byte-compile explicitly: the automatic pass only walks %%{python3_sitelib},
# and the payload deliberately lives outside it. Skipping this does not just
# cost one slow first start -- %%{_datadir} is not user-writable, so the
# interpreter cannot save __pycache__ and recompiles every module on *every*
# launch. Guarded so a distro without python-rpm-macros still builds.
%{?py_byte_compile:%py_byte_compile %{python3} %{buildroot}%{_datadir}/%{name}}

%find_lang %{name} || touch %{name}.lang

# Runs the desktop-entry and AppStream validators defined in data/meson.build.
%check
%meson_test


%files -f %{name}.lang
%license LICENSE*
%doc README*
%{_bindir}/sshpilot
%{_bindir}/sshpilot-agent
%{_datadir}/%{name}/
%{_datadir}/io.github.mfat.sshpilot/
%{_datadir}/applications/io.github.mfat.sshpilot.desktop
%{_metainfodir}/io.github.mfat.sshpilot.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.mfat.sshpilot.svg
# Glob the compression suffix: rpm gzips man pages, but which suffix it uses is
# a distro policy, not something this spec should hardcode.
%{_mandir}/man1/sshpilot.1*
%{_mandir}/man1/sshpilot-agent.1*

%changelog
* Mon Jul 20 2026 mFat <newmfat@gmail.com> - 5.5.9-1
- Meson build system
- Migrated to GNOME rcommended project structure

* Mon Jul 20 2026 mFat <newmfat@gmail.com> - 5.5.8
- Migrated to meson build system

* Sun Jul 19 2026 mFat <newmfat@gmail.com> - 5.5.7
- Enhanced support for multi-step authentication challenges
- Better support for FIDO hardware keys
- More robust password autofill flow
- Redesigned SCP dialog

* Sun Jul 19 2026 mFat <newmfat@gmail.com> - 5.5.6
- Enhanced suppot for multi-sttep authentication challenges
- Better support for FIDO hardware keys
- More robust password autofill flow
- Revamped SCP dialogs

* Sat Jul 18 2026 mFat <newmfat@gmail.com> - 5.5.5
- Bug fixes and minor UI improvements

* Fri Jul 17 2026 mFat <newmfat@gmail.com> - 5.5.4
- Bug fixes

* Fri Jul 17 2026 mFat <newmfat@gmail.com> - 5.5.3
- Improvements for Xterm.js terminal backend
- Bug fixes

* Wed Jul 15 2026 mFat <newmfat@gmail.com> - 5.5.2
- Bug fixes

* Tue Jul 14 2026 mFat <newmfat@gmail.com> - 5.5.1
- Docker Console UI improvements
- File manager UI improvements
- Reorganized main menu
- Bug fixes

* Mon Jul 13 2026 mFat <newmfat@gmail.com> - 5.5.0
- Backup settings and credentials to Bitwarden/Vaultwarden right from SSH Pilot and restore on other devices
- More credential storage backends including Keepass, Bitwarden and rbw
- Improvements to Pyxtermjs terminal backend
- Various bug fixes and performance improvements

* Sat Jun 27 2026 mFat <newmfat@gmail.com> - 5.4.6
- Fixes SCP bug in Flatpak
- Adds "Edit as Root" button to text editor. Edit eny remote files with sudo
- Better logging and diagnostics suite

* Fri Jun 26 2026 mFat <newmfat@gmail.com> - 5.4.5
- Improved drag and drop and group nesting experience

* Fri Jun 26 2026 mFat <newmfat@gmail.com> - 5.4.4
- Added support for nested groups
- Improvements to Drag & Drop experience in connection list
- Fixed scp failing to download directories
- Bug fixes

* Wed Jun 24 2026 mFat <newmfat@gmail.com> - 5.4.3
- Bug fixes
- Performance improvements for SFTP file manager
- Added option to copy text on select

* Wed Jun 24 2026 mFat <newmfat@gmail.com> - 5.4.2
- Bug fixes

* Wed Jun 24 2026 mFat <newmfat@gmail.com> - 5.4.1
- New Docker/Podman management console
- New plugin feature + plugin SDk with a bunch of experimental plugins
- Support for additional protocols (mosh, serial, etc.)
- UI fixes and improvements

* Wed Jun 24 2026 mFat <newmfat@gmail.com> - 5.4.0
- Docker/Podman container management console
- New plugin framework and SDK
- New plugin settings to install and manage plugins
- Improved SSH config editor
- UI fixes and improvements
- Additional protocols

* Fri Jun 12 2026 mFat <newmfat@gmail.com> - 5.3.0
- New "Tags" feature: add tags to connections
- Search now supports tags
- New tags view for the connection list
- Drag and drop a connection on a tag group to apply tag
- Added inline autocompletion for tags
- Added inline autocompletion for jump hosts
- Added support for selecting connections with space key

* Thu Jun 11 2026 mFat <newmfat@gmail.com> - 5.2.9
- Bug fixes

* Wed Jun 10 2026 mFat <newmfat@gmail.com> - 5.2.8
- Updated ssh-copy-id dialog
- Fixes macOS keychain prompting repeatedly
- Minor UI improvements and bug fixes

* Wed Jun 10 2026 mFat <newmfat@gmail.com> - 5.2.7
- Bug fixes

* Wed Jun 10 2026 mFat <newmfat@gmail.com> - 5.2.6
- Improved connection editor
- Bug fixes

* Tue Jun 09 2026 mFat <newmfat@gmail.com> - 5.2.5
- Bug fixes

* Tue Jun 09 2026 mFat <newmfat@gmail.com> - 5.2.4
- Bug fixes and UI improvements

* Sun Jun 07 2026 mFat <newmfat@gmail.com> - 5.2.3
- Added fallback for older libadwaita versions failing to load connection editor

* Sun Jun 07 2026 mFat <newmfat@gmail.com> - 5.2.2
- Bug fixes

* Sun Jun 07 2026 mFat <newmfat@gmail.com> - 5.2.1
- Bug fixes

* Sat Jun 06 2026 mFat <newmfat@gmail.com> - 5.2.0
- Drag and drop a connection on an existing terminal to create a split view

* Wed Jun 03 2026 mFat <newmfat@gmail.com> - 5.1.7
- Bug fixes and improvements

* Tue Jun 02 2026 mFat <newmfat@gmail.com> - 5.1.5
- Added type-ahead search - open the app, start typing and press enter. The first matching host will be connected
- Made it easier to switch between the terminal and connection list with Ctrl+Shift+L
- Bug fixes

* Tue Jun 02 2026 mFat <newmfat@gmail.com> - 5.1.4
- Better look and feel for built-in file manager
- Zoom control for file manager
- New option to always show Commands sidebar
- More reliable SFTP file transfers
- Changed default connection list shortcut to Ctrl+Shift+L
- Various bug fixes and UI improvements

* Tue Jun 02 2026 mFat <newmfat@gmail.com> - 5.1.3
- Better look and feel for built-in file manager

* Mon Jun 01 2026 mFat <newmfat@gmail.com> - 5.1.1
- Bug fixes

* Mon Jun 01 2026 mFat <newmfat@gmail.com> - 5.1.0
- Introducing Command Blocks - Organize your favorite commands in folders, insert them into terminal with a simple double-click
- Choose a command from your command snippet intventory to run on a single host or a group of machines
- More improvements to Split View

* Sat May 30 2026 mFat <newmfat@gmail.com> - 5.0.0
- New "Sessions" feature — Save, open, rename, and delete snapshots of your open tabs and restore them automatically
- Pin saved sessions to the start page for one-click restore
- Copy to group — Add a connection to another group without removing it from its current group(s). Same connection can appear in multiple groups.
- Jump host picker — Pick jump hosts from your saved connections when editing a connection.
- Better Split View - Resize terminal panes freely, drag and drop an entire group onto a Split View
- Minor UI fixes and improvements

* Fri May 29 2026 mFat <newmfat@gmail.com> - 4.9.2
- - Bug fixes

* Fri May 29 2026 mFat <newmfat@gmail.com> - 4.9.0
- - New "Splt View" feature - view terminals side by side inside any terminal tab
- - New context menu item to copy host address

* Mon May 25 2026 mFat <newmfat@gmail.com> - 4.8.3
- - Updated flatpak to GNOME Platform 50

* Mon May 25 2026 mFat <newmfat@gmail.com> - 4.8.2
- - Added URL support to terminal
- - Bug fixes

* Sun May 24 2026 mFat <newmfat@gmail.com> - 4.8.1
- - Bug fixes

* Sat May 23 2026 mFat <newmfat@gmail.com> - 4.8.0
- - Fixes for Quick Connect function
- - Add option to save Quick Connect host
- - Added option to rename a tab - Double-click any terminal tab to rename
- - Added support for Wake on Lan
- - Added pre-connection command support

* Sat Dec 27 2025 mFat <newmfat@gmail.com> - 4.7.9
- - Minor bug fixes

* Fri Dec 12 2025 mFat <newmfat@gmail.com> - 4.7.8
- - Bug fixes

* Fri Dec 12 2025 mFat <newmfat@gmail.com> - 4.7.7
- - Fixed saved secrets not used for login

* Thu Dec 11 2025 mFat <newmfat@gmail.com> - 4.7.6
- - Support for sidebar resizing
- - Toggles for info labels in sidebar
- - Updated color badges
- - Various bug fixes and UI improvements

* Thu Dec 11 2025 mFat <newmfat@gmail.com> - 4.7.5
- - Bug fixes

* Wed Dec 10 2025 mFat <newmfat@gmail.com> - 4.7.4
- - Added option to resize sidebar
- - Fixed long host values making sidebar too wide
- - Bug fixes and UI improvements

* Wed Dec 10 2025 mFat <newmfat@gmail.com> - 4.7.3
- - Drag and drop files and folders on terminal to upload via SCP

* Wed Dec 10 2025 mFat <newmfat@gmail.com> - 4.7.2
- - Bug fixes

