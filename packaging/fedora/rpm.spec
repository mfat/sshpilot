Name:           sshpilot
Version:        %{?version}%{!?version:4.6.4}
Release:        1%{?dist}
Summary:        SSH connection manager with integrated terminal

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/sshpilot
Source0:        https://github.com/mfat/sshpilot/archive/refs/tags/v%{version}.tar.gz



BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils

# Exclude automatic Python ABI dependency to allow compatibility across Python 3.x versions
%global __requires_exclude ^python\\(abi\\)

Requires:       python3
Requires:       python3-gobject
Requires:       gtk4 >= 4.6
Requires:       libadwaita >= 1.4
Requires:       vte291-gtk4 >= 0.70
Requires:       gtksourceview5 >= 5.0
Requires:       python3-paramiko
Requires:       python3-cryptography
Requires:       python3-secretstorage 
Requires:       libsecret
Requires:       sshpass
Requires:       openssh-askpass
Requires:       webkitgtk6

%description
SSH Pilot is a user-friendly SSH connection manager featuring built-in tabbed terminal, remote file management, key transfer, port forwarding and more. It's an alternative to Putty, Termius and Mobaxterm.


%prep
%autosetup -n sshpilot-%{version}


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

# Install desktop file and icon
install -D -m 644 io.github.mfat.sshpilot.desktop %{buildroot}%{_datadir}/applications/io.github.mfat.sshpilot.desktop
install -D -m 644 sshpilot/resources/sshpilot.svg %{buildroot}%{_datadir}/pixmaps/io.github.mfat.sshpilot.svg

%check
# Validate desktop file
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.mfat.sshpilot.desktop || true

%files
%license LICENSE*
%doc README*
%{_bindir}/sshpilot
%{python3_sitelib}/sshpilot/
%{_datadir}/applications/io.github.mfat.sshpilot.desktop
%{_datadir}/pixmaps/io.github.mfat.sshpilot.svg

%changelog
* Tue Dec 02 2025 mFat <newmfat@gmail.com> - 4.6.4
- - Bug fixes

* Tue Dec 02 2025 mFat <newmfat@gmail.com> - 4.6.3
- - Fixed black toolbar in file manager
- - Fixed nano editor issue when run under KDE Plasma desktop

* Thu Nov 27 2025 mFat <newmfat@gmail.com> - 4.6.0
- - Improvements to built-in file manager, new text editor
- - Added file mmanager button to connection rows
- - Sort button now sorts groups too

* Mon Nov 24 2025 mFat <newmfat@gmail.com> - 4.5.1
- - Fixed file manager bug when password authentication is seected

* Mon Nov 24 2025 mFat <newmfat@gmail.com> - 4.5.0
- - Redesigned start page, with card and list layouts
- - Minor UI fixes

* Sat Nov 22 2025 mFat <newmfat@gmail.com> - 4.4.4
- - Update notifier fixes

* Sat Nov 22 2025 mFat <newmfat@gmail.com> - 4.4.3
- - Bug fixes

* Sat Nov 22 2025 mFat <newmfat@gmail.com> - 4.4.2
- - Added update notifier

* Fri Nov 21 2025 mFat <newmfat@gmail.com> - 4.4.1
- - New feature: Import/Export configuration
- - Better log output

* Wed Oct 08 2025 mFat <newmfat@gmail.com>
- Automated COPR build
