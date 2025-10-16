**SSH Pilot** is a user-friendly, modern and lightweight SSH connection manager for Linux and macOS, with an integrated terminal and a file manager.

## Table of Contents

- [Features](#features)
- [Download](#download)
  - [Debian/Ubuntu APT Repository](#--debianubuntu-apt-repository)
  - [Debian/Ubuntu (Manual Install)](#--debianubuntu-manual-install)
  - [Fedora/RHEL/openSUSE COPR Repository](#-fedorarhel-opensuse-copr-repository)
  - [Fedora/RHEL/openSUSE (Manual Install)](#-fedorarhel-opensuse-manual-install)
  - [Flatpak](#-flatpak)
  - [Arch Linux](#-arch-linux)
  - [macOS](#-macos-aarch64)
- [Minimum Requirements](#minimum-requirements)
- [Run from Source](#-run-from-source)
- [Runtime Dependencies](#runtime-dependencies)
- [Testing](#testing)
- [Telegram Channel](#telegram-channel)
- [Special Thanks](#special-thanks)
- [Support Development](#support-development)

<img width="1322" height="922" alt="Screenshot From 2025-10-07 10-57-55" src="https://github.com/user-attachments/assets/af8ce903-2704-4740-8e39-547765ddd490" />


<table>
  <tr>
    <td align="center" valign="top">
      <img src="screenshots/start-page.png" width="560" alt="Start Page"><br><strong>Start Page</strong>
    </td>
    <td align="center" valign="top">
      <img src="screenshots/main-window-with-tabs.png" width="560" alt="Main Window with Tabs"><br><strong>Main Window with Tabs</strong>
    </td>
    <td align="center" valign="top">
      <img src="screenshots/tab-overview.png" width="560" alt="Tab Overview"><br><strong>Tab Overview</strong>
    </td>
  </tr>
  <tr>
    <td></td> <!-- empty cell left -->
    <td align="center" valign="top">
      <img src="screenshots/ssh-copy-id.png" width="560" alt="SSH Copy ID"><br><strong>SSH Copy ID</strong>
    </td>
    <td></td> <!-- empty cell right -->
  </tr>
</table>






## Features

- Tabbed interface
- Intuitive, minimal UI with keyboard navigation and shortcuts
- File management using SFTP
- Organize servers in groups
- Option to use the built-in terminal or your favorite one
- Broadcast commands to all open tabs
- Full support for Local, Remote and Dynamic port forwarding 
- SCP support for quickly uploading or downloading files to/from remote servers
- Keypair generation and copying to remote servers (ssh-copy-id)
- Support for running remote and local commands upon login
- Secure storage for credentials via libsecret on Linux; no secret (password or passphrase) is copied to clipboard or saved to plain text
- Privacy toggle to show/hide ip addresses/hostnames in the main window
- Light/Dark interface themes
- Customizable terminal font and color schemes
- Load/save standard .ssh/config entries (Or use dedicated configuration file)
- Free software (GPL v3 license)


## Download

### <img src="https://img.icons8.com/color/48/000000/debian.png" width="24"/> <img src="https://img.icons8.com/color/48/000000/ubuntu.png" width="24"/> Debian/Ubuntu APT Repository

#### Installation

1. Add the GPG key:
```bash
curl -fsSL https://mfat.github.io/sshpilot-ppa/pubkey.gpg | sudo gpg --dearmor -o /usr/share/keyrings/sshpilot-ppa.gpg
```

2. Add the repository:
```bash
echo "deb [signed-by=/usr/share/keyrings/sshpilot-ppa.gpg arch=amd64] https://mfat.github.io/sshpilot-ppa any main" | sudo tee /etc/apt/sources.list.d/sshpilot-ppa.list
```

3. Update and install:
```bash
sudo apt update
sudo apt install sshpilot
```

For more information, visit: https://mfat.github.io/sshpilot-ppa/

### <img src="https://img.icons8.com/color/48/000000/debian.png" width="24"/> <img src="https://img.icons8.com/color/48/000000/ubuntu.png" width="24"/> Debian/Ubuntu (Manual Install)
Latest release can be downloaded from here: https://github.com/mfat/sshpilot/releases/

### <img src="https://upload.wikimedia.org/wikipedia/commons/3/3f/Fedora_logo.svg" width="24" height="24"/> Fedora/RHEL/openSUSE COPR Repository

This repository provides automatic updates for SSH Pilot on RPM-based distributions.

```bash
dnf copr enable mahdif62/sshpilot
dnf install sshpilot
```

[![Copr build status](https://copr.fedorainfracloud.org/coprs/mahdif62/sshpilot/package/sshpilot/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/mahdif62/sshpilot/package/sshpilot/)

### <img src="https://upload.wikimedia.org/wikipedia/commons/3/3f/Fedora_logo.svg" width="24" height="24"/> Fedora/RHEL/openSUSE (Manual Install)
Latest release can be downloaded from here: https://github.com/mfat/sshpilot/releases/

### <img src="https://flathub.org/api/badge?locale=en" width="24" height="24"/> Flatpak
Available on [Flathub](https://flathub.org/en/apps/io.github.mfat.sshpilot)

<p align="center">
<a href='https://flathub.org/apps/io.github.mfat.sshpilot'>
    <img width='240' alt='Get it on Flathub' src='https://flathub.org/api/badge?locale=en'/>
  </a>
</p>

OR in a terminal type: 

```
flatpak install flathub io.github.mfat.sshpilot
```

### <img src="https://img.icons8.com/color/48/000000/arch-linux.png" width="24"/> Arch Linux
Arch Linux package via AUR: https://aur.archlinux.org/packages/sshpilot

```
paru -S sshpilot
```
or

```
yay -S sshpilot
```

### <img src="https://upload.wikimedia.org/wikipedia/commons/7/74/Apple_logo_dark_grey.svg" width="24" height="24"/> macOS (aarch64)
Download the dmg file from the releases section https://github.com/mfat/sshpilot/releases/

---

## Minimum Requirements

| Component | Minimum Version |
|-----------|----------------|
| GTK 4 | 4.6 |
| libadwaita | 1.4 |
| VTE (GTK4) | 0.70 |
| PyGObject | 3.42 |
| pycairo | 1.20.0 |
| Paramiko | 3.4 |
| cryptography | 42.0 |
| keyring | 24.3 |
| psutil | 5.9.0 |

---

### 💻 Run from Source
You can also run the app from source. Install the modules listed in requirements.txt and a fairly recent version of GNOME and it should run.

`
python3 run.py
`

To enable verbose debugging output, run the app with the `--verbose` flag:

`
python3 run.py --verbose
`



## Runtime Dependencies

Install system GTK/libadwaita/VTE GI bindings (do not use pip for these).

Debian/Ubuntu (minimum versions)

```
sudo apt update
sudo apt install \
  python3 python3-gi python3-gi-cairo \
  libgtk-4-1 (>= 4.6) gir1.2-gtk-4.0 (>= 4.6) \
  libadwaita-1-0 (>= 1.4) gir1.2-adw-1 (>= 1.4) \
  libvte-2.91-gtk4-0 (>= 0.70) gir1.2-vte-3.91 (>= 0.70) \
  libsecret-1-0 gir1.2-secret-1 \
  python3-paramiko python3-cryptography sshpass ssh-askpass
```

Fedora / RHEL / CentOS


```
sudo dnf install \
  python3 python3-gobject \
  gtk4 libadwaita \
  vte291-gtk4 \
  libsecret \
  python3-paramiko python3-cryptography sshpass openssh-askpass
```

libsecret handles secure credential storage on Linux via the Secret Service API.

Run from source


```
python3 run.py
```

Enable verbose debugging with:

```
python3 run.py --verbose
```

## Testing

- **Unit & integration:**

  ```bash
  pytest -m "not e2e"
  ```

- **End-to-end (Dogtail, requires X11/AT-SPI):**

  ```bash
  dbus-run-session -- xvfb-run -s "-screen 0 1024x768x24" pytest -m e2e tests_e2e
  ```

## Telegram Channel
https://t.me/sshpilot

## Special Thanks

- [Elibugy](https://www.linkedin.com/in/elham-hesaraki) as the primary sponsor of the project
- Behnam Tavakkoli, Chalist and Kalpase, Ramin Najjarbashi, Farid and Narbeh for testing
- Icon designed by [Blisterexe](https://github.com/Blisterexe)

## Support Development

Ko-fi: https://ko-fi.com/newmfat

Bitcoin:

```
bc1qqtsyf0ft85zshsnw25jgsxnqy45rfa867zqk4t
```

Doge:
```
DRzNb8DycFD65H6oHNLuzyTzY1S5avPHHx
```
USDT (TRC20)
```
TAvQWVD83DB3QuDspnMh4uiJ7hi4Jzcr6X
```

