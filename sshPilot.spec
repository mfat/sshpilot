%global srcname sshpilot
%global appid io.github.mfat.sshpilot

Name:           sshPilot
Version:        1.0.0
Release:        1%{?dist}
Summary:        SSH connection manager with integrated terminal

License:        GPL-3.0
URL:            https://github.com/mfat/sshpilot
Source0:        https://github.com/mfat/sshpilot/archive/v%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel >= 3.10
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  python3-pip
BuildRequires:  meson >= 0.59.0
BuildRequires:  glib2-devel
BuildRequires:  gtk4-devel >= 4.6
BuildRequires:  libadwaita-devel >= 1.2
BuildRequires:  vte291-gtk4-devel >= 0.70
BuildRequires:  gobject-introspection-devel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

Requires:       python3 >= 3.10
Requires:       python3-gobject >= 3.42
Requires:       gtk4 >= 4.6
Requires:       libadwaita >= 1.2
Requires:       vte291-gtk4 >= 0.70
Requires:       python3-paramiko >= 3.4
Requires:       python3-pyyaml >= 6.0
Requires:       python3-secretstorage >= 3.3
Requires:       python3-cryptography >= 42.0
Requires:       python3-matplotlib >= 3.8
Requires:       openssh-clients

Recommends:     openssh-askpass
Suggests:       openssh-server

%description
sshPilot is a modern SSH connection manager built with GTK4 and libadwaita.
It provides a user-friendly interface for managing SSH connections with
advanced features including:

* Tabbed interface with integrated VTE terminal
* Secure password storage using system keyring
* SSH key generation and management
* SSH tunneling support (local, remote, dynamic)
* Real-time resource monitoring with charts
* X11 forwarding support
* Terminal themes and customization
* SSH config file integration

sshPilot follows GNOME Human Interface Guidelines and integrates seamlessly
with modern Linux desktop environments.

%prep
%autosetup -n %{srcname}-%{version}

%build
%py3_build

%install
%py3_install

# Install desktop file
desktop-file-install \
    --dir=%{buildroot}%{_datadir}/applications \
    data/%{appid}.desktop

# Install appdata file
install -Dm644 data/%{appid}.appdata.xml \
    %{buildroot}%{_datadir}/metainfo/%{appid}.appdata.xml

# Install icon
install -Dm644 src/io.github.mfat.sshpilot/resources/sshpilot.png \
    %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/%{appid}.png

# Install GSchema
install -Dm644 data/%{appid}.gschema.xml \
    %{buildroot}%{_datadir}/glib-2.0/schemas/%{appid}.gschema.xml

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
appstream-util validate-relax --nonet %{buildroot}%{_datadir}/metainfo/%{appid}.appdata.xml

%files
%license LICENSE
%doc README.md
%{python3_sitelib}/io.github.mfat.sshpilot/
%{python3_sitelib}/sshPilot-%{version}-py%{python3_version}.egg-info/
%{_bindir}/sshpilot
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/metainfo/%{appid}.appdata.xml
%{_datadir}/icons/hicolor/256x256/apps/%{appid}.png
%{_datadir}/glib-2.0/schemas/%{appid}.gschema.xml

%post
/usr/bin/glib-compile-schemas %{_datadir}/glib-2.0/schemas &> /dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &> /dev/null || :
/usr/bin/update-desktop-database &> /dev/null || :

%postun
/usr/bin/glib-compile-schemas %{_datadir}/glib-2.0/schemas &> /dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &> /dev/null || :
/usr/bin/update-desktop-database &> /dev/null || :

%changelog
* Thu Aug 01 2025 mFat <newmfat@gmail.com> - 1.0.0-1
- Initial release of sshPilot
- Modern GTK4/libadwaita interface following GNOME HIG
- Integrated VTE terminal with tabbed interface
- SSH connection management with config file integration
- Secure password storage using system keyring
- SSH key generation and deployment capabilities
- SSH tunneling support (local, remote, dynamic)
- Real-time resource monitoring with matplotlib charts
- X11 forwarding support
- Terminal themes and customization options
- Comprehensive keyboard shortcuts