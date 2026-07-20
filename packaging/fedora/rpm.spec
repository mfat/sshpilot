Name:           sshpilot
Version:        %{?version}%{!?version:5.5.7}
Release:        1%{?dist}
Summary:        Manage your servers with ease

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/sshpilot
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz


BuildArch:      noarch
BuildRequires:  meson >= 0.59
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


# Exclude automatic Python ABI dependency to allow compatibility across Python 3.x versions
%global __requires_exclude ^python\\(abi\\)

# Define _metainfodir for openSUSE compatibility (not defined by default on openSUSE)
%{!?_metainfodir:%global _metainfodir %{_datadir}/metainfo}

Requires:       python3
Requires:       python3-gobject
Requires:       gtk4 >= 4.6
# 1.5 for Adw.Dialog / Adw.AlertDialog, both used unguarded.
Requires:       libadwaita >= 1.5
Requires:       vte291-gtk4 >= 0.70
Requires:       gtksourceview5 >= 5.0
Requires:       python3-cryptography
Requires:       python3-secretstorage 
Requires:       python3-flask
Requires:       python3-flask-socketio
Requires:       libsecret
Requires:       sshpass
Requires:       openssh-askpass
Requires:       webkitgtk6.0
# Optional: KeePass (.kdbx) secret backend (degrades gracefully if absent).
Recommends:     python3-pykeepass
# For the built-in telnet protocol plugin (degrades gracefully if absent)
Recommends:     telnet

%description
SSH Pilot is a user-friendly SSH connection manager featuring built-in tabbed terminal, remote file management, key transfer, port forwarding and more. It's an alternative to Putty, Termius and Mobaxterm.

%prep
%autosetup -n %{name}-%{version}

# Meson installs the launcher, the Python package, the compiled GResource, the
# desktop entry and AppStream metainfo (merged from the .in templates), the icon
# and sshpilot-agent. Nothing is replayed by hand here.
%build
%meson
%meson_build

%install
%meson_install

# po/LINGUAS is still empty, so nothing is installed under %%{_datadir}/locale and
# %%find_lang fails; the fallback keeps the build green until the first
# translation lands, at which point the .mo files get packaged automatically
# instead of tripping unpackaged-files.
%find_lang %{name} || touch %{name}.lang

# Runs the desktop-entry and AppStream validators defined in data/meson.build.
%check
%meson_test


%files -f %{name}.lang
%license LICENSE*
%doc README*
%{_bindir}/sshpilot
%{_bindir}/sshpilot-agent
%{python3_sitelib}/sshpilot/
%{_datadir}/io.github.mfat.sshpilot/
%{_datadir}/applications/io.github.mfat.sshpilot.desktop
%{_metainfodir}/io.github.mfat.sshpilot.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.mfat.sshpilot.svg

%changelog
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

