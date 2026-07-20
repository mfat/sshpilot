**SSH Pilot** is a user-friendly SSH and SFTP client for Linux and macOS



## Features

- Works with your existing ~/.ssh/config
- Integrated terminal with tabbed interface and split view support (works with your favorite terminal apps too)
- Intuitive, minimal UI with keyboard navigation and customizable shortcuts
- Built-in SFTP dual-pane file manager
- SCP download/upload support
- Key transfer to server (ssh-copy-id)
- Known hosts and Authorized keys management (local & remote)
- Graphical Docker container managemer 
- Support for various secure storage backends (libsecret, Keepass, Bitwarden/Vaultwarden, pass)
- Backup and restore to/from Bitwarden/Vaultwarden, or your own servers (supports app settings, secrets and keys)
- Snippets for quick execution of scripts/commands
- Extensible via plugins

## Download

You can download directly from the [Releases](https://github.com/mfat/sshpilot/releases/) section, but we recomment adding below repositories for automatic updates:

### <img src="https://img.icons8.com/color/48/000000/ubuntu.png" width="24"/> Ubuntu PPA

```bash
sudo add-apt-repository ppa:mfat/sshpilot
sudo apt update
sudo apt install sshpilot
```

### <img src="https://img.icons8.com/color/48/000000/debian.png" width="24"/> Debian APT Repository

1. Add the GPG key:
```bash
curl -fsSL https://mfat.github.io/sshpilot-ppa/pubkey.gpg | sudo gpg --dearmor -o /usr/share/keyrings/sshpilot-ppa.gpg
```

2. Add the repository:
```bash
echo "deb [signed-by=/usr/share/keyrings/sshpilot-ppa.gpg] https://mfat.github.io/sshpilot-ppa any main" | sudo tee /etc/apt/sources.list.d/sshpilot-ppa.list
```

3. Update and install:
```bash
sudo apt update
sudo apt install sshpilot
```

For more information, visit: https://mfat.github.io/sshpilot-ppa/


### <img src="https://upload.wikimedia.org/wikipedia/commons/3/3f/Fedora_logo.svg" width="24" height="24"/> Fedora/RHEL COPR Repository

This repository provides automatic updates for SSH Pilot on RPM-based distributions.

```bash
dnf copr enable mahdif62/sshpilot
dnf install sshpilot
```

[![Copr build status](https://copr.fedorainfracloud.org/coprs/mahdif62/sshpilot/package/sshpilot/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/mahdif62/sshpilot/package/sshpilot/)


### <img src="https://flathub.org/favicon.svg" width="24" height="24"/> Flatpak
Available on [Flathub](https://flathub.org/en/apps/io.github.mfat.sshpilot)

<p align="left">
<a href='https://flathub.org/apps/io.github.mfat.sshpilot'>
    <img width='160' alt='Get it on Flathub' src='https://flathub.org/api/badge?locale=en'/>
  </a>
</p>

OR in a terminal type: 

```bash
flatpak install flathub io.github.mfat.sshpilot
```

### <img src="https://img.icons8.com/color/48/000000/arch-linux.png" width="24"/> Arch Linux
Arch Linux package via AUR: https://aur.archlinux.org/packages/sshpilot

```bash
# replace yay with your AUR helper of choice, e.g. paru
yay -S sshpilot
```

OR

Nightly Arch Linux package via AUR (community maintained): https://aur.archlinux.org/packages/sshpilot-git

```bash
# replace yay with your AUR helper of choice, e.g. paru
yay -S sshpilot-git
```

### <img src="https://brew.sh/assets/img/homebrew-256x256.png" width="24" height="24"/> Homebrew (macOS + Linux)

```bash
brew tap mfat/sshpilot
brew install sshpilot
```

More info here: https://github.com/mfat/homebrew-sshpilot

Works on macOS Homebrew and Linuxbrew. The formula is build-from-source; first install pulls the GTK4 stack and compiles a Python virtualenv with the runtime deps. After install, launch sshPilot from a terminal inside an active desktop session (Wayland/X11 + dbus on Linux; native on macOS).

### <img src="https://upload.wikimedia.org/wikipedia/commons/7/74/Apple_logo_dark_grey.svg" height="24"/> macOS (aarch64 and x86-64)
Download the dmg file from the releases section https://github.com/mfat/sshpilot/releases/

### For development on macOS:
```bash
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass gtksourceview5
```

**Note:** `webkitgtk` is Linux-only and not available on macOS via Homebrew. The PyXterm.js backend will not be available on macOS; the application will use the VTE backend instead.



---

## Minimum Requirements

### Operating system

| Platform | Minimum version |
|----------|-----------------|
| Debian | 13 (trixie) |
| Ubuntu | 24.04 (noble) |
| Linux Mint | 22 |
| Fedora | 43 |
| RHEL / CentOS Stream | 10 |
| Arch Linux | rolling |
| macOS | 14 (Sonoma) |

On any other Linux distribution, install the [Flatpak](#-flatpak) — it runs
everywhere Flatpak does.

### Libraries

| Component    | Minimum Version |
|---------------|----------------|
| GTK 4         | 4.6            |
| libadwaita    | 1.5            |
| VTE (GTK4)    | 0.70           |
| PyGObject     | 3.42           |
| pycairo       | 1.20.0         |
| cryptography  | 42.0           |
| keyring       | 24.3           |
| psutil        | 5.9.0          |
| pykeepass     | 4.0            |
| certifi       | 2023.7.22      |
| GtkSourceView | 5.0            |

---

### Run from Source

[docs/running-from-source.md](docs/running-from-source.md).


## Documentation
- User guide and FAQ: https://github.com/mfat/sshpilot/wiki
- Architecture reference: [docs/architecture.md](docs/architecture.md)
- Diagnostics & logging: [docs/diagnostics.md](docs/diagnostics.md)
- In-repo developer and platform docs: [docs/](docs/)
- Writing plugins (protocols & UI pages): [docs/plugins/](docs/plugins/writing-plugins.md)

## Telegram Channel
https://t.me/sshpilot

## Third-Party Libraries

SSH Pilot uses the following third-party libraries:

- **[pyxtermjs](https://github.com/cs01/pyxtermjs)** - A fully functional terminal in your browser, used as an alternative terminal backend (MIT License)

## Special Thanks

- [Elibugy](https://www.linkedin.com/in/elham-hesaraki), [Mo Efazati](https://github.com/efazati) and [Sadeq](https://github.com/sadeq-n-yazdi) for supporting development
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

