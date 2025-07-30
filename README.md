# SSH Manager

A modern GTK-based SSH client with terminal integration, built with Python and PyGObject.

## Features

- **Connection Management**: Save and manage multiple SSH connections
- **Tabbed Interface**: Open each connection in a separate tab
- **Authentication Support**: Both password and SSH key authentication
- **SSH Key Passphrase Support**: Automatic handling of password-protected SSH keys
- **Auto Key Detection**: Automatically detects SSH keys in `~/.ssh/`
- **Visual Status**: Green for connected hosts, grey for inactive
- **Connection Controls**: Connect, disconnect, rename, and delete connections
- **Terminal Integration**: Full Vte.Terminal integration for each connection

## Screenshots

The application features a clean, modern interface with:
- Left panel: Connection list with status indicators
- Right panel: Tabbed terminal interface
- Control buttons for managing connections

## Installation

### Quick Installation

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
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-vte-3.91 libgirepository1.0-dev python3-paramiko python3-cryptography
```

#### Fedora:
```bash
sudo dnf install python3-gobject gtk4 vte291 python3-paramiko python3-cryptography
```

#### Arch Linux:
```bash
sudo pacman -S python-gobject gtk4 vte3 python-paramiko python-cryptography
```

### Testing Installation

Run the installation test to verify everything is working:

```bash
python3 test_installation.py
```

## Usage

### Running the Application

You can run the SSH Manager in several ways:

1. **Direct execution:**
   ```bash
   python3 fresh_ssh_manager.py
   ```

2. **Using the launcher script (recommended):**
   ```bash
   ./run_ssh_manager.sh
   ```

3. **From the applications menu** (if installed via install.sh)

> **Note**: If you're in a Python virtual environment, use the launcher script `./run_ssh_manager.sh` as it automatically handles virtual environment deactivation. GTK/PyGObject requires system-level packages that aren't available in virtual environments.

### Demo Script

Run the demo script to see how the SSH Manager works programmatically:

```bash
python3 demo.py
```

This will create sample connections and demonstrate the API usage.

### Adding a Connection

1. Click the "Add" button
2. Fill in the connection details:
   - **Connection Name**: A friendly name for the connection
   - **Hostname**: The remote server's hostname or IP address
   - **Username**: Your username on the remote server
   - **Port**: SSH port (default: 22)
   - **Authentication**: Choose between password or SSH key
   - **Key Passphrase**: If using an SSH key protected with a passphrase, enter it here

### SSH Key Passphrase Support

The SSH Manager supports password-protected SSH keys with automatic passphrase handling:

- **Automatic Agent Integration**: If `ssh-agent` is running, the application will attempt to add your key automatically
- **Tool Detection**: Uses `sshpass` or `expect` if available for seamless passphrase handling
- **Fallback**: If automation tools aren't available, SSH will prompt for the passphrase in the terminal
- **Security**: Passphrases are stored securely and only used when establishing connections

For optimal experience, install additional tools:
```bash
# Ubuntu/Debian
sudo apt install sshpass expect

# Fedora
sudo dnf install sshpass expect
```

### Managing Connections

- **Connect**: Select a connection and click "Connect" to open it in a new tab
- **Disconnect**: Select a connected host and click "Disconnect" to close the connection
- **Rename**: Change the display name of a saved connection
- **Delete**: Remove a connection from the list (will disconnect if active)

### Tab Management

- Each SSH connection opens in a new tab
- If a connection is already open, clicking "Connect" will switch to its existing tab
- Close tabs using the X button on each tab
- Tabs are automatically managed and cleaned up when connections are closed

## Configuration

The application automatically saves your connections to `~/.ssh_manager_config.json`. This file contains:

- Connection names and details
- Hostnames and usernames
- Authentication methods (passwords are stored in plain text - consider using SSH keys for security)

## Security Notes

- Passwords are stored in plain text in the configuration file
- For better security, use SSH key authentication
- The application automatically detects SSH keys in your `~/.ssh/` directory
- Consider using SSH agents for key management

## Troubleshooting

### Common Issues

1. **GTK/Vte not found**: Make sure you have the correct system packages installed
2. **Terminal not spawning**: Check that the SSH command is available in your PATH
3. **Permission denied**: Ensure your SSH keys have the correct permissions (600)

### Debug Mode

Run with verbose output to debug connection issues:

```bash
python3 ssh_manager.py --debug
```

## Development

### Project Structure

```
GMyServers/
├── ssh_manager.py          # Main application
├── test_installation.py    # Installation test script
├── demo.py                 # Demo script showing API usage
├── install.sh              # Automated installation script
├── run_ssh_manager.sh      # Launcher script
├── requirements.txt        # Python dependencies (for reference)
└── README.md              # This file
```

### Key Components

- `SSHManager`: Main application class
- `SSHConnection`: Connection data model
- `AddConnectionDialog`: Dialog for adding new connections
- `RenameDialog`: Dialog for renaming connections

### API Usage

The SSH Manager can be used programmatically:

```python
from ssh_manager import SSHManager, SSHConnection

# Create manager
manager = SSHManager()

# Add connection
conn = SSHConnection(
    hostname="example.com",
    username="user",
    port=22,
    name="My Server"
)
manager.connections.append(conn)
manager.save_connections()

# Run GUI
manager.run()
```

## Troubleshooting

### "No module named 'gi'" Error

If you encounter this error, it means you're trying to run the application from within a Python virtual environment. GTK/PyGObject is a system package and not available in virtual environments.

**Solutions:**
1. **Use the launcher script** (recommended):
   ```bash
   ./run_ssh_manager.sh
   ```
   
2. **Deactivate the virtual environment manually**:
   ```bash
   deactivate
   python3 fresh_ssh_manager.py
   ```

3. **Exit the virtual environment completely**:
   ```bash
   exit  # or open a new terminal
   python3 fresh_ssh_manager.py
   ```

### SSH Key Passphrase Issues

If SSH key passphrases aren't working automatically:

1. **Install automation tools** (optional but recommended):
   ```bash
   sudo apt install sshpass expect  # Ubuntu/Debian
   sudo dnf install sshpass expect  # Fedora
   ```

2. **Ensure ssh-agent is running**:
   ```bash
   eval $(ssh-agent -s)
   ```

3. **Manual key addition**:
   ```bash
   ssh-add ~/.ssh/your_key_file
   ```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is open source. Feel free to modify and distribute according to your needs.

## Acknowledgments

- Built with GTK4 and PyGObject
- Uses Vte.Terminal for terminal emulation
- Inspired by modern SSH clients like PuTTY and MobaXterm 