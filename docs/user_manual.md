# sshPilot User Manual

## Table of Contents

1. [Installation](#installation)
2. [Getting Started](#getting-started)
3. [Managing Connections](#managing-connections)
4. [SSH Key Management](#ssh-key-management)
5. [Terminal Features](#terminal-features)
6. [SSH Tunneling](#ssh-tunneling)
7. [Resource Monitoring](#resource-monitoring)
8. [Preferences](#preferences)
9. [Keyboard Shortcuts](#keyboard-shortcuts)
10. [Troubleshooting](#troubleshooting)

## Installation

### System Requirements

- Linux distribution with GTK 4.6+ and libadwaita 1.2+
- Python 3.10 or newer
- SSH client (`openssh-client` package)

### Installation Methods

#### Flatpak (Recommended)

```bash
flatpak install flathub io.github.mfat.sshpilot
```

#### Debian/Ubuntu

```bash
sudo dpkg -i sshpilot_1.0.0-1_all.deb
sudo apt-get install -f
```

#### Fedora/RHEL

```bash
sudo dnf install sshpilot-1.0.0-1.noarch.rpm
```

#### From Source

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
pip install -r requirements.txt
python setup.py install --user
```

## Getting Started

### First Launch

1. Launch sshPilot from your application menu or run `sshpilot` in terminal
2. You'll see the welcome screen with keyboard shortcuts
3. The left sidebar shows your connection list (initially empty)
4. The main area displays tabs for terminal sessions

### Adding Your First Connection

1. Click the **+** button in the sidebar or press **Ctrl+N**
2. Fill in the connection details:
   - **Nickname**: A friendly name for this connection
   - **Hostname**: IP address or domain name
   - **Username**: Your username on the remote host
   - **Port**: SSH port (default: 22)
3. Choose authentication method:
   - **Password**: Enter your password (stored securely)
   - **SSH Key**: Browse to select your private key file
   - **SSH Agent**: Use keys loaded in ssh-agent
4. Click **Save**

### Connecting to a Host

- **Double-click** a connection in the list
- Select a connection and press **Enter**
- Right-click and select "Connect"

## Managing Connections

### Connection List

The sidebar displays all your saved connections with:
- Computer icon
- Nickname (bold)
- Username@hostname (dimmed)
- Connection status indicator

### Connection Operations

#### Adding Connections
- Click **+** button or press **Ctrl+N**
- Import from existing `~/.ssh/config` file (automatic)

#### Editing Connections
- Select connection and click edit button
- Right-click connection → "Edit"
- Modify any connection properties

#### Deleting Connections
- Select connection and click trash button
- Right-click connection → "Delete"
- Confirm deletion in dialog

#### Reordering Connections
- Drag and drop connections to reorder
- Use up/down buttons in context menu

### Connection Properties

#### Basic Settings
- **Nickname**: Display name in connection list
- **Hostname**: Target server address
- **Username**: Login username
- **Port**: SSH port number

#### Authentication
- **Password**: Stored securely in system keyring
- **SSH Key**: Path to private key file
- **SSH Agent**: Use agent-loaded keys

#### Advanced Settings
- **X11 Forwarding**: Enable GUI application support
- **SSH Tunnel**: Configure port forwarding
- **Compression**: Enable SSH compression

## SSH Key Management

### Generating Keys

1. Press **Ctrl+Shift+K** or use menu → "Generate SSH Key"
2. Configure key properties:
   - **Key Name**: Filename (e.g., `id_rsa`)
   - **Key Type**: RSA or Ed25519
   - **Key Size**: 2048, 3072, or 4096 bits (RSA only)
   - **Comment**: Optional description
   - **Passphrase**: Optional key protection
3. Click **Generate**

Keys are saved to `~/.ssh/` directory.

### Deploying Keys

#### Automatic Deployment
1. Generate or select existing key
2. In connection dialog, choose "SSH Key" authentication
3. Browse to select the key file
4. sshPilot will attempt to copy the public key on first connection

#### Manual Deployment
```bash
ssh-copy-id -i ~/.ssh/id_rsa.pub user@hostname
```

### Key Management Features

- **Auto-detection**: Automatically finds keys in `~/.ssh/`
- **Key validation**: Verifies key file integrity
- **Passphrase management**: Secure passphrase storage
- **SSH agent integration**: Add/remove keys from agent

## Terminal Features

### Terminal Interface

Each connection opens in a separate tab with:
- Full VTE terminal emulation
- Scrollback buffer (configurable)
- Copy/paste support
- Search functionality
- Context menu

### Terminal Operations

#### Text Operations
- **Copy**: Select text and right-click → "Copy" or **Ctrl+Shift+C**
- **Paste**: Right-click → "Paste" or **Ctrl+Shift+V**
- **Select All**: Right-click → "Select All" or **Ctrl+Shift+A**

#### Terminal Control
- **Reset**: Right-click → "Reset"
- **Clear**: Right-click → "Reset and Clear"
- **Search**: **Ctrl+Shift+F** (if supported)

### Tab Management

- **New Tab**: Connect to same host creates new tab
- **Close Tab**: Click × button or disconnect
- **Switch Tabs**: Click tab or use **Ctrl+PageUp/PageDown**
- **Reorder Tabs**: Drag tabs to reorder

### Terminal Themes

Built-in themes:
- **Default**: Standard black/white
- **Dark**: Modern dark theme
- **Light**: Clean light theme
- **Solarized Dark**: Popular dark color scheme
- **Solarized Light**: Popular light color scheme

Custom themes can be created in preferences.

## SSH Tunneling

### Tunnel Types

#### Local Forward (-L)
Forwards local port to remote service:
```
Local Port → SSH Server → Remote Service
```
Use case: Access remote database locally

#### Remote Forward (-R)
Forwards remote port to local service:
```
Remote Port → SSH Server → Local Service
```
Use case: Expose local web server remotely

#### Dynamic Forward (-D)
Creates SOCKS proxy:
```
Local SOCKS Proxy → SSH Server → Internet
```
Use case: Secure web browsing through remote server

### Configuring Tunnels

1. In connection dialog, select tunnel type
2. Enter tunnel port number
3. Save and connect
4. Tunnel is active while connected

### Tunnel Examples

#### Access Remote MySQL
- Type: Local Forward
- Port: 3306
- Access: `mysql -h localhost -P 3306`

#### Expose Local Web Server
- Type: Remote Forward  
- Port: 8080
- Remote users access via server:8080

#### SOCKS Proxy
- Type: Dynamic Forward
- Port: 1080
- Configure browser to use localhost:1080 as SOCKS proxy

## Resource Monitoring

### Overview

Monitor system resources on connected hosts:
- CPU usage percentage
- Memory usage (used/total)
- Disk usage percentage
- Network I/O rates

### Accessing Monitor

1. Connect to a host
2. Press **Ctrl+R** or use menu → "Show Resource Monitor"
3. New tab opens with real-time charts

### Chart Features

- **Real-time updates**: Data refreshes every 5 seconds (configurable)
- **Historical data**: Shows last 5 minutes by default
- **Multiple metrics**: CPU, RAM, disk, network in separate charts
- **Auto-scaling**: Y-axis adjusts to data range

### Configuration

In Preferences → Monitoring:
- **Enable/disable monitoring**
- **Update interval**: 1-60 seconds
- **History length**: 60-3600 data points

### Requirements

Remote host must have standard Unix commands:
- `top` (CPU usage)
- `free` (memory usage)
- `df` (disk usage)
- `/proc/net/dev` (network statistics)

## Preferences

Access via **Ctrl+,** or menu → "Preferences"

### Terminal Settings

#### Appearance
- **Color Theme**: Choose from built-in or custom themes
- **Font**: Select font family and size

#### Behavior  
- **Scrollback Lines**: History buffer size (100-100,000)
- **Cursor Blinking**: Enable/disable cursor blink
- **Audible Bell**: Sound on terminal bell

### Interface Settings

#### Window
- **Remember Window Size**: Restore size on startup
- **Auto Focus Terminal**: Focus terminal when connecting
- **Confirm Tab Closure**: Show confirmation dialog

#### Connection List
- **Show Hostname**: Display hostname in connection list

### SSH Settings

#### Connection
- **Connection Timeout**: Timeout in seconds (5-300)
- **Keepalive Interval**: Keepalive interval (0-600)
- **Enable Compression**: Use SSH compression
- **Auto Add Host Keys**: Accept unknown host keys automatically

#### Security
- **Store Passwords**: Save passwords in system keyring
- **SSH Agent Forwarding**: Forward SSH agent to remote host

### Monitoring Settings

- **Enable Resource Monitoring**: Turn monitoring on/off
- **Update Interval**: How often to fetch data (1-60 seconds)
- **History Length**: Number of data points to keep (60-3600)

## Keyboard Shortcuts

### Global Shortcuts
| Shortcut | Action |
|----------|--------|
| **Ctrl+N** | New Connection |
| **Ctrl+L** | Toggle Connection List Focus |
| **Ctrl+Shift+K** | Generate SSH Key |
| **Ctrl+R** | Show Resource Monitor |
| **Ctrl+,** | Preferences |
| **Ctrl+Q** | Quit Application |

### Connection List
| Shortcut | Action |
|----------|--------|
| **Enter** | Connect to Selected Host |
| **Delete** | Delete Selected Connection |
| **F2** | Edit Selected Connection |
| **↑/↓** | Navigate Connections |

### Terminal
| Shortcut | Action |
|----------|--------|
| **Ctrl+Shift+C** | Copy |
| **Ctrl+Shift+V** | Paste |
| **Ctrl+Shift+A** | Select All |
| **Ctrl+Shift+F** | Find |
| **Ctrl+Shift+T** | New Tab (same host) |
| **Ctrl+Shift+W** | Close Tab |
| **Ctrl+PageUp/PageDown** | Switch Tabs |

### Tab Management
| Shortcut | Action |
|----------|--------|
| **Ctrl+T** | New Tab |
| **Ctrl+W** | Close Tab |
| **Ctrl+1-9** | Switch to Tab N |
| **Alt+1-9** | Switch to Tab N |

## Troubleshooting

### Connection Issues

#### "Connection refused"
- Check hostname/IP address
- Verify SSH service is running on remote host
- Check firewall settings
- Ensure correct port number

#### "Permission denied"
- Verify username is correct
- Check authentication method
- For key auth: verify key file permissions (`chmod 600 ~/.ssh/id_rsa`)
- For password auth: ensure password is correct

#### "Host key verification failed"
- Host key has changed (security warning)
- Remove old key: `ssh-keygen -R hostname`
- Or enable "Auto Add Host Keys" in preferences

### Terminal Issues

#### Blank terminal after connection
- Check SSH connection status in connection list
- Try connecting manually: `ssh user@host`
- Check logs in `~/.local/share/sshPilot/sshpilot.log`

#### Characters not displaying correctly
- Check terminal encoding settings
- Verify locale settings on remote host
- Try different terminal font

#### Copy/paste not working
- Use **Ctrl+Shift+C/V** instead of **Ctrl+C/V**
- Check if text is actually selected
- Try right-click context menu

### Resource Monitoring Issues

#### No monitoring data
- Ensure remote host has required commands (`top`, `free`, `df`)
- Check SSH connection stability
- Verify monitoring is enabled in preferences

#### Charts not updating
- Check update interval in preferences
- Verify SSH connection is active
- Look for errors in application logs

### Performance Issues

#### Slow connection
- Enable SSH compression in connection properties
- Check network connectivity
- Consider using SSH keys instead of passwords

#### High CPU usage
- Disable resource monitoring if not needed
- Reduce monitoring update frequency
- Close unused terminal tabs

### Debug Information

#### Enable Debug Logging
```bash
SSHPILOT_DEBUG=1 sshpilot
```

#### Log Locations
- **Application logs**: `~/.local/share/sshPilot/sshpilot.log`
- **Configuration**: `~/.config/sshpilot/`
- **SSH config**: `~/.ssh/config`

#### Getting Help
- **GitHub Issues**: https://github.com/mfat/sshpilot/issues
- **Email**: newmfat@gmail.com
- **Include**: sshPilot version, OS version, error messages, log files

### Common Solutions

#### Reset Configuration
```bash
rm -rf ~/.config/sshpilot/
```

#### Reset SSH Configuration
```bash
# Backup first
cp ~/.ssh/config ~/.ssh/config.backup
# Edit manually or delete problematic entries
```

#### Reinstall Application
```bash
# Flatpak
flatpak uninstall io.github.mfat.sshpilot
flatpak install flathub io.github.mfat.sshpilot

# Debian/Ubuntu
sudo apt remove sshpilot
sudo dpkg -i sshpilot_1.0.0-1_all.deb
```