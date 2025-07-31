# SSH Pilot

A simple GTK-based SSH client.

## Features

- **Connection Management**: Save and manage multiple SSH connections
- **Tabbed Interface**: Open each connection in a separate tab
- **Authentication Support**: Both password and SSH key authentication
- **Auto Key Detection**: Automatically detects SSH keys in `~/.ssh/`
- **Custom Themes and Fonts for Terminal**: Use your desired style for the terminal

## Screenshots

<img width="1322" height="959" alt="Screenshot From 2025-08-01 02-12-58" src="https://github.com/user-attachments/assets/a25a8536-1cf6-4692-b429-50e15e6fe052" />

<img width="548" height="609" alt="Screenshot From 2025-08-01 02-15-57" src="https://github.com/user-attachments/assets/9e0bd766-bb12-4e6d-ad00-c11a0bd7ac4f" />


<img width="508" height="523" alt="Screenshot From 2025-08-01 02-16-39" src="https://github.com/user-attachments/assets/261875da-c15e-4279-b75b-f7f0cd65875c" />



## Installation

There is a debian package provided in the [releases](https://github.com/mfat/sshpilot/releases) section

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
