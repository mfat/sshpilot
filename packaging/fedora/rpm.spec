Name:           sshpilot
Version:        %{?version}%{!?version:4.4.2}
Release:        1%{?dist}
Summary:        SSH connection manager with integrated terminal

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/sshpilot
Source0:        https://github.com/mfat/sshpilot/archive/refs/heads/dev.tar.gz

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
Requires:       python3-paramiko
Requires:       python3-cryptography
Requires:       python3-secretstorage 
Requires:       libsecret
Requires:       sshpass
Requires:       openssh-askpass

%description
SSH Pilot is a user-friendly SSH connection manager featuring built-in tabbed terminal, remote file management, key transfer, port forwarding and more. It's an alternative to Putty, Termius and Mobaxterm.


%prep
%autosetup -n sshpilot-dev

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
* Sat Nov 22 2025 mFat <newmfat@gmail.com> - 4.4.2
- - Added update notifier

* Fri Nov 21 2025 mFat <newmfat@gmail.com> - 4.4.1
- - New feature: Import/Export configuration
- - Better log output

* Wed Oct 08 2025 mFat <newmfat@gmail.com>
- Automated COPR build
