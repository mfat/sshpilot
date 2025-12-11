Name:           sshpilot
Version:        %{?version}%{!?version:4.7.7}
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

