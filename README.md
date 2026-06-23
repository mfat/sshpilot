**SSH Pilot** is a user-friendly, modern and lightweight SSH connection manager for Linux and macOS, with an integrated terminal and SFTP file manager. It's an alternative to Termius, Putty, Mobaxterm and similar apps.






<table>
  <tr>
    <td align="center" valign="top">
      <img width="1409" height="1092" alt="Start Page" src="https://github.com/user-attachments/assets/99670247-8456-45dd-bff8-af80592324f5" />
    </td>
    
  </tr>
</table>

- [About](#about)
- [Features](#features)
- [Download](#download)
  - [Debian/Ubuntu APT Repository](#--debianubuntu-apt-repository)
  - [Debian/Ubuntu (Manual Install)](#--debianubuntu-manual-install)
  - [Fedora/RHEL/openSUSE COPR Repository](#-fedorarhelopensuse-copr-repository)
  - [Fedora/RHEL/openSUSE (Manual Install)](#-fedorarhelopensuse-manual-install)
  - [Flatpak](#-flatpak)
  - [Arch Linux](#-arch-linux)
  - [Homebrew (macOS + Linux)](#-homebrew-macos--linux)
  - [macOS](#-macos-aarch64)
- [Minimum Requirements](#minimum-requirements)
- [Run from Source](#-run-from-source)
- [Documentation](#documentation)
- [Telegram Channel](#telegram-channel)
- [Third-Party Libraries](#third-party-libraries)
- [Special Thanks](#special-thanks)
- [Support Development](#support-development)

  
## About

### What is SSH Pilot?
It's an SSH connection manager with an integrated terminal and built-in dual-pane SFTP client.

### Why should I use SSH Pilot?
It makes managing multiple machines easier and more fun. You see all your hosts in one unified interface and can organize them into groups, with color tags.

### What makes it unique?
SSH Pilot is a GUI on top of your .ssh/config
It honors your existing SSH configuration. Just fire up the app and you'll be have access to all your machines instantly.

### What else can it do?
It can do [so many things](#features).

SSH Pilot can generate and copy keys to your servers.
It stores your secrets (passwords and private key passphrases) securely in the operating system's keychain.
It can securely use saved secrets to log you in.


## Features

- Tabbed interface
- Intuitive, minimal UI with keyboard navigation and shortcuts
- Built-in SFTP dual-pane file manager
- Organize servers in groups
- Option to use the built-in terminal or your favorite one
- Broadcast commands to all open tabs
- Full support for Local, Remote and Dynamic port forwarding 
- SCP support for quickly uploading or downloading files to/from remote servers
- Keypair generation and copying to remote servers (ssh-copy-id)
- Support for running remote and local commands upon login
- Secure storage for credentials
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

### <img src="https://upload.wikimedia.org/wikipedia/commons/7/74/Apple_logo_dark_grey.svg" height="24"/> macOS (aarch64)
Download the dmg file from the releases section https://github.com/mfat/sshpilot/releases/

### For development on macOS:
```bash
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass gtksourceview5
```

**Note:** `webkitgtk` is Linux-only and not available on macOS via Homebrew. The PyXterm.js backend will not be available on macOS; the application will use the VTE backend instead.



---

## Minimum Requirements

| Component    | Minimum Version |
|---------------|----------------|
| GTK 4         | 4.6            |
| libadwaita    | 1.4            |
| VTE (GTK4)    | 0.70           |
| PyGObject     | 3.42           |
| pycairo       | 1.20.0         |
| Paramiko      | 3.4            |
| cryptography  | 42.0           |
| keyring       | 24.3           |
| psutil        | 5.9.0          |
| GtkSourceView | 5.0            |

---

### 💻 Run from Source

sshPilot is run from source in a **Python virtual environment (venv) + pip**, on
top of the GTK stack. Two setups are supported (both mirror PyGObject's official
[Getting Started](https://pygobject.gnome.org/getting_started.html) guide):

- **Hybrid (recommended):** install the GTK stack *and* PyGObject from your
  distribution, then use a `--system-site-packages` venv for the pure-Python
  deps. No compiler needed — the quick-start below uses this.
- **Pure venv:** build PyGObject/pycairo from PyPI into a plain venv (needs a C
  toolchain + GTK `-dev` headers).

📖 **Full guide to both approaches, dev/test setup, and troubleshooting:**
[documentation/running-from-source.md](documentation/running-from-source.md).

#### ⚡ Quick install (automated)

One command — detects your distro (Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE),
installs the system GTK stack via `sudo`, sets up the venv, and launches:

```bash
git clone https://github.com/mfat/sshpilot.git && cd sshpilot && ./scripts/install-run-linux.sh
```

(Flags: `--no-run`, `--with-webkit`, `-y`. On an unsupported distro, follow the
manual steps below.) Prefer to set things up yourself? Continue with Step 1.

> **Why a venv?** Modern Linux distributions ship an *externally-managed* system
> Python (PEP 668) that refuses `pip install`. A venv keeps sshPilot's Python
> dependencies isolated.

> **Why system packages for the GTK stack (hybrid)?** PyGObject, pycairo, and
> the GTK4/libadwaita/VTE runtime are provided by your distribution. **Do not
> install PyGObject or pycairo via pip** in this setup — pip would build them
> from source, requiring a C toolchain and `-dev` headers. Install them as system
> packages (Step 1) and create the venv with `--system-site-packages` so it can
> see them (Step 2).

#### Step 1 — Install system prerequisites (required)

These provide PyGObject, the GObject-Introspection (GI) typelibs, and the native
GTK4/libadwaita/VTE runtime. Install them **before** creating the venv.

**Debian/Ubuntu**

```bash
sudo apt update
sudo apt install \
  python3 python3-venv python3-gi python3-gi-cairo \
  libgtk-4-1 gir1.2-gtk-4.0 \
  libadwaita-1-0 gir1.2-adw-1 \
  libvte-2.91-gtk4-0 gir1.2-vte-3.91 \
  libgtksourceview-5-0 gir1.2-gtksource-5 \
  libsecret-1-0 gir1.2-secret-1 \
  python3-paramiko python3-cryptography sshpass ssh-askpass \
  gir1.2-webkit-6.0
```

**Fedora / RHEL / CentOS**

```bash
sudo dnf install \
  python3 python3-gobject \
  gtk4 libadwaita \
  vte291-gtk4 \
  gtksourceview5 \
  libsecret \
  python3-paramiko python3-cryptography sshpass openssh-askpass \
  webkitgtk6
```

**Arch Linux**

```bash
sudo pacman -S --needed \
  python python-gobject python-cairo \
  gtk4 libadwaita vte4 gtksourceview5 libsecret \
  python-paramiko python-cryptography sshpass
```

**openSUSE (Tumbleweed)**

```bash
sudo zypper install \
  python3 python3-gobject \
  typelib-1_0-Gtk-4_0 \
  gtk4 libadwaita \
  typelib-1_0-Adw-1 \
  typelib-1_0-Vte-3_91 \
  typelib-1_0-GtkSource-5 \
  typelib-1_0-Secret-1 \
  python3-paramiko python3-cryptography \
  sshpass openssh-askpass-gnome
```

Other distributions work too — install the equivalent GTK4 / libadwaita / VTE
(GTK4) / GtkSourceView 5 / libsecret packages plus PyGObject. The optional
**WebKit 6.0** package (`gir1.2-webkit-6.0` / `webkitgtk6` / `webkitgtk-6.0` /
`typelib-1_0-WebKit-6_0`) is only needed for the PyXterm.js terminal backend; the
default VTE backend works without it. Full per-distro detail (and the pure-venv
approach) is in
[documentation/running-from-source.md](documentation/running-from-source.md).

`libsecret` handles secure credential storage on Linux via the Secret Service
API. macOS contributors should follow
[documentation/INSTALL-macos.md](documentation/INSTALL-macos.md) for the
Homebrew GTK stack instead.

#### Step 2 — Create and activate a virtual environment

Create the venv **with `--system-site-packages`** so it can use the
distribution's PyGObject/pycairo and GI bindings from Step 1:

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
```

(Run `deactivate` to leave the environment later.)

#### Step 3 — Install the Python dependencies (pip, inside the venv)

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs only the pure-Python dependencies (Paramiko, cryptography,
keyring, psutil, …). PyGObject and pycairo are intentionally **not** installed
here — they come from the system packages in Step 1.

#### Step 4 — Run

```bash
python3 run.py
```

Enable verbose debugging output with the `--verbose` flag:

```bash
python3 run.py --verbose
```

Prefer to keep PyGObject out of system packages? The **pure-venv** approach
(pip-built PyGObject/pycairo in a plain venv) is documented in the
[full source-install guide](documentation/running-from-source.md#approach-b--pure-venv-pip-built-pygobject).

> **Alternative (not for development):** if you only want to *use* sshPilot, the
> distribution packages, Flatpak, Homebrew, and AUR builds in
> [Download](#download) are the easiest route. The venv workflow above is the
> recommended path for running the latest source and for contributing.

## Documentation
- User guide and FAQ: https://github.com/mfat/sshpilot/wiki
- In-repo developer and platform docs: [documentation/](documentation/)
- Writing plugins (protocols & UI pages): [docs/plugins/](docs/plugins/writing-plugins.md)

## Telegram Channel
https://t.me/sshpilot

## Third-Party Libraries

SSH Pilot uses the following third-party libraries:

- **[pyxtermjs](https://github.com/cs01/pyxtermjs)** - A fully functional terminal in your browser, used as an alternative terminal backend (MIT License)

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

