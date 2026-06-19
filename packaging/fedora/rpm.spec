Name:           sshpilot
Version:        %{?version}%{!?version:5.3.0}
Release:        1%{?dist}
Summary:        Manage your servers with ease

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/sshpilot
Source0:        https://github.com/mfat/sshpilot/archive/refs/heads/main.tar.gz


BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
#BuildRequires:  libappstream-glib


# Exclude automatic Python ABI dependency to allow compatibility across Python 3.x versions
%global __requires_exclude ^python\\(abi\\)

# Define _metainfodir for openSUSE compatibility (not defined by default on openSUSE)
%{!?_metainfodir:%global _metainfodir %{_datadir}/metainfo}

Requires:       python3
Requires:       python3-gobject
Requires:       gtk4 >= 4.6
Requires:       libadwaita >= 1.4
Requires:       vte291-gtk4 >= 0.70
Requires:       gtksourceview5 >= 5.0
Requires:       python3-paramiko
Requires:       python3-cryptography
Requires:       python3-secretstorage 
Requires:       python3-flask
Requires:       python3-flask-socketio
Requires:       libsecret
Requires:       sshpass
Requires:       openssh-askpass
Requires:       webkitgtk6.0
# For the built-in telnet protocol plugin (degrades gracefully if absent)
Recommends:     telnet

%description
SSH Pilot is a user-friendly SSH connection manager featuring built-in tabbed terminal, remote file management, key transfer, port forwarding and more. It's an alternative to Putty, Termius and Mobaxterm.

%prep
%autosetup -n sshpilot-main

%build
# No build step needed - standalone Python application

%install
# Show directory structure for debugging
ls -la
ls -la sshpilot/ || echo "sshpilot directory check"

# Install the main executable
install -D -m 755 run.py %{buildroot}%{_bindir}/sshpilot

# Install Python modules
install -d %{buildroot}%{python3_sitelib}/sshpilot
cp -a sshpilot/*.py %{buildroot}%{python3_sitelib}/sshpilot/

# Plugin subpackage (loader, registry, built-in protocols + their plugin.json).
# Example plugins are dev references only — never shipped.
cp -a sshpilot/plugins %{buildroot}%{python3_sitelib}/sshpilot/
rm -rf %{buildroot}%{python3_sitelib}/sshpilot/plugins/examples
find %{buildroot}%{python3_sitelib}/sshpilot/plugins -name __pycache__ -type d -prune -exec rm -rf {} +

# Install resources
install -d %{buildroot}%{python3_sitelib}/sshpilot/resources
cp -a sshpilot/resources/* %{buildroot}%{python3_sitelib}/sshpilot/resources/

# Install vendored pyxtermjs module
install -d %{buildroot}%{python3_sitelib}/sshpilot/vendor
cp -a sshpilot/vendor/__init__.py %{buildroot}%{python3_sitelib}/sshpilot/vendor/
install -d %{buildroot}%{python3_sitelib}/sshpilot/vendor/pyxtermjs
cp -a sshpilot/vendor/pyxtermjs/*.py %{buildroot}%{python3_sitelib}/sshpilot/vendor/pyxtermjs/
cp -a sshpilot/vendor/pyxtermjs/*.html %{buildroot}%{python3_sitelib}/sshpilot/vendor/pyxtermjs/ 2>/dev/null || true
cp -a sshpilot/vendor/pyxtermjs/LICENSE %{buildroot}%{python3_sitelib}/sshpilot/vendor/pyxtermjs/ 2>/dev/null || true

# Install desktop file and icon
install -D -m 644 io.github.mfat.sshpilot.desktop %{buildroot}%{_datadir}/applications/io.github.mfat.sshpilot.desktop
install -D -m 644 io.github.mfat.sshpilot.metainfo.xml %{buildroot}%{_metainfodir}/io.github.mfat.sshpilot.metainfo.xml
# Install icon to hicolor theme (per AppStream guidelines)
install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -D -m 644 sshpilot/resources/sshpilot.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.mfat.sshpilot.svg

%check
# Validate desktop file
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.mfat.sshpilot.desktop
#appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.mfat.sshpilot.metainfo.xml


%files
%license LICENSE*
%doc README*
%{_bindir}/sshpilot
%{python3_sitelib}/sshpilot/
%{_datadir}/applications/io.github.mfat.sshpilot.desktop
%{_metainfodir}/io.github.mfat.sshpilot.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.mfat.sshpilot.svg

%changelog
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

