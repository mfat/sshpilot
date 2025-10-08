Name:           sshpilot
Version:        {{{ git_dir_version }}}
Release:        1%{?dist}
Summary:        SSH connection manager with integrated terminal

License:        GPL-3.0-or-later
URL:            https://github.com/mfat/sshpilot
VCS:            {{{ git_dir_vcs }}}
Source:         {{{ git_dir_pack }}}

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils

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
sshPilot provides SSH connection management, integrated terminal using VTE,
tunneling, key management, and tabbed interface. Built with GTK4 and Adwaita
for a modern Linux desktop experience.

%prep
{{{ git_dir_setup_macro }}}

%build
# No build step needed - standalone Python application

%install
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
{{{ git_dir_changelog }}}
