# SSH Pilot

A simple GTK-based SSH client.

## Features

- **Connection Management**: Save and manage multiple SSH connections
- **Tabbed Interface**: Open each connection in a separate tab
- **Authentication Support**: Both password and SSH key authentication
- **Auto Key Detection**: Automatically detects SSH keys in `~/.ssh/`
- **Custom Themes and Fonts for Terminal**: Use your desired style for the terminal

## Screenshots



## Installation

There is a debian package provided in the releases section

For other distros, use the install script as below:

Run the automated installation script:

```bash
./install.sh
```

This will install all required dependencies and create a desktop shortcut.

### Manual Installation

#### Prerequisites

Make sure you have the following system dependencies installed:

#### Ubuntu/Debian:
```bash
sudo apt update
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-vte-3.91 libgirepository1.0-dev python3-paramiko python3-cryptography gir1.2-secret-1
```

#### Fedora:
```bash
sudo dnf install python3-gobject gtk4 vte291 python3-paramiko python3-cryptography libsecret-devel libsecret
```

#### Arch Linux:
```bash
sudo pacman -S python-gobject gtk4 vte3 python-paramiko python-cryptography libsecret
```



## Security Notes

- Passwords are stored in GNOME's secure storage



## License

This project is FREE (as in freedom) software. Feel free to modify and distribute according to your needs.

## Acknowledgments

- Built with GTK4 and PyGObject
- Uses Vte.Terminal
