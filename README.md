<p align="center">
<img width="154" height="154" alt="logo" src="https://github.com/user-attachments/assets/42b73dbf-778c-45ff-9361-22a52988f1b3" />
</p>

**sshPilot** is a user-friendly, modern and lightweight SSH connection manager for Linux. It's a free (as in freedom) alternative to Putty and Termius.

<img width="1057" height="705" alt="Screenshot From 2025-08-20 18-32-09" src="https://github.com/user-attachments/assets/f57b25a9-c3ce-4355-891e-caad17a906f9" />

<img width="1212" height="778" alt="Screenshot From 2025-08-15 01-22-02" src="https://github.com/user-attachments/assets/6b79a06a-d900-49eb-969f-a8f7a4c31b02" />

<img width="762" height="995" alt="Screenshot From 2025-08-15 01-18-57" src="https://github.com/user-attachments/assets/aec20f9a-1fb5-44bb-a13a-bb5a36445431" />

<img width="722" height="622" alt="Screenshot From 2025-08-15 01-17-38" src="https://github.com/user-attachments/assets/b72fe4df-f5ac-48e2-9ba0-af728901e1c8" />

<img width="562" height="569" alt="Screenshot From 2025-08-20 13-49-59" src="https://github.com/user-attachments/assets/eb8de65b-ce0e-449e-a7e3-dcc6bf1e43bb" />


## Features

- Tabbed interface
- Full support for Local, Remote and Dynamic port forwarding 
- Intuitive, minimal UI with keyboard navigation and shortcuts
- SCP support for quicly uploading a file to remote server
- Keypair generation and copying to remote servers (ssh-copy-id)
- Support for running remote and local commands upon login
- Secure storage for credentials, no secret (password or passphrase) is copied to clipboard or saved to plain text
- Privacy toggle to show/hide ip addresses/hostnames in the main window
- Light/Dark interface themes
- Customizable terminal font and color schemes
- Load/save standard .ssh/config entries
- Free software (GPL v3 license)

## Installation 

### Linux
The app is currently distributed as deb and rpm packages (see releases) and can be installed on recent versions of Debian (testing/unstable), Ubuntu and Fedora. Debian bookworm is not supported due to older libadwaita version. A flatpak is also provided that should work on any distro with flatpak support. (Do NOT install deb/rpm and Flatpak together as the desktop launcher will not work)
[Download](https://github.com/mfat/sshpilot/releases/)

### macOS

#### Option 1: Using the Launcher Script (Recommended)
```bash
# Make the launcher executable and run it
chmod +x sshpilot-mac.sh
./sshpilot-mac.sh
```

The launcher script will:
- Check and install required dependencies via Homebrew
- Set up environment variables automatically
- Test the setup and launch the application

#### Option 2: Manual Setup
```bash
# Install dependencies
brew install gtk4 libadwaita vte3 adwaita-icon-theme gobject-introspection pygobject3 sshpass

# Set up environment variables
export BREW_PREFIX=$(brew --prefix)
export PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
export PYTHONPATH="$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages:$PYTHONPATH"
export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib:$DYLD_LIBRARY_PATH"
export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"

# Run the application
python3 run.py
```

#### Option 3: Development Setup
```bash
# Use the development setup script
./setup_dev_env.sh
```

## Features

- SSH connection management
- Integrated terminal
- Key management
- Port forwarding
- Resource monitoring

## Building

### macOS Application Bundles

The project includes GitHub Actions workflows to build macOS application bundles:

- **py2app**: Creates `.app` bundles using py2app
- **PyInstaller**: Creates `.app` bundles using PyInstaller
- **Nuitka**: Creates standalone executables
- **GTK4**: Multi-platform GTK4 builds

Workflows are triggered on pushes to the `mac` branch or manually via GitHub Actions.

## Development

### Prerequisites

- Python 3.11+
- GTK4 and libadwaita
- VTE terminal widget
- PyGObject bindings

### Installation

1. Clone the repository
2. Install dependencies (see Quick Start section)
3. Run the application

### Environment Variables

The following environment variables are required for PyGObject and GTK4:

```bash
export PYTHONPATH="$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages:$PYTHONPATH"
export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib:$DYLD_LIBRARY_PATH"
export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"
```

## License

[Add your license information here]

## Download

Latest release can be downloaded from here: https://github.com/mfat/sshpilot/releases/

If your distro doesn't use DEB or RPM, the Flatpak version should work. 

You can also run the app from source. Install the modules listed in requirements.txt and a fairly recent version of GNOME and it should run.

`
python3 run.py
`




Runtime dependencies
--------------------

Install system GTK/libadwaita/VTE GI bindings (do not use pip for these).

Debian/Ubuntu (minimum versions)

```
sudo apt update
sudo apt install \
  python3 python3-gi python3-gi-cairo \
  libgtk-4-1 (>= 4.6) gir1.2-gtk-4.0 (>= 4.6) \
  libadwaita-1-0 (>= 1.4) gir1.2-adw-1 (>= 1.4) \
  libvte-2.91-gtk4-0 (>= 0.70) gir1.2-vte-3.91 (>= 0.70) \
  python3-paramiko python3-cryptography python3-secretstorage sshpass ssh-askpass
```

Fedora / RHEL / CentOS


```
sudo dnf install \
  python3 python3-gobject \
  gtk4 libadwaita \
  vte291-gtk4 \
  libsecret \
  python3-paramiko python3-cryptography python3-secretstorage sshpass openssh-askpass
```

Run from source


```
python3 run.py
```



## Keyboard/mouse navigation and shortcuts

sshPilot is easy to navigate with keyboard. When the app starts up, just press enter to connect to the first host in the list. You can do the same thing by double-clicking the host.
Press ctrl+L to quickly switch between hosts, close tabs with ctrl+F4 and switch tabs with alt+right/left arrow.
If you have multiple connections to a single host, doble-clicking the host will cycle through all its open tabs.

## Special Thanks

- [Elibugy](https://www.linkedin.com/in/elham-hesaraki) as the primary sponsor of the project
- Behnam Tavakkoli, Chalist and Kalpase for testing
- Icon designed by [Blisterexe](https://github.com/Blisterexe)

## Support development
Bitcoin: bc1qqtsyf0ft85zshsnw25jgsxnqy45rfa867zqk4t

Doge: DRzNb8DycFD65H6oHNLuzyTzY1S5avPHHx
