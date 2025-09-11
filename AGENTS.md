# AGENTS.md

## Project Overview

sshPilot is a user-friendly, modern SSH connection manager with an integrated terminal for Linux, macOS, and Windows. It's built with Python, GTK4, and libadwaita, providing a native desktop experience.

## Setup Commands

- Install dependencies: `pip install -r requirements.txt`
- Run from source: `python3 run.py`
- Run with verbose debugging: `python3 run.py --verbose`
- Run tests: `pytest`
- Build PyInstaller bundle: `./pyinstaller.sh` (macOS)

## Development Environment

### System Dependencies
Install GTK4/libadwaita/VTE system packages 

**Debian/Ubuntu:**
```bash
sudo apt install python3-gi python3-gi-cairo libgtk-4-1 gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 libvte-2.91-gtk4-0 gir1.2-vte-3.91 python3-paramiko python3-cryptography python3-secretstorage sshpass ssh-askpass
```

**Fedora/RHEL/CentOS:**
```bash
sudo dnf install python3-gobject gtk4 libadwaita vte291-gtk4 libsecret python3-paramiko python3-cryptography python3-secretstorage sshpass openssh-askpass
```

### Python Dependencies
- Python >= 3.8
- PyGObject >= 3.42
- pycairo >= 1.20.0
- paramiko >= 3.4
- cryptography >= 42.0
- secretstorage >= 3.3 (Linux only)
- keyring >= 24.3
- psutil >= 5.9.0

## Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) for Python code style
- Use type hints where appropriate
- Prefer GTK4/Adwaita components over custom widgets and follow GNOME HIG guidelines. Prefer modern Adwaita UI elements over traditional GTK
- All UI should be defined in code, not UI files

## Testing Instructions

- Run `pytest` to execute the test suite before committing changes
- Add or update tests when modifying code
- Verify keyboard shortcuts work on both platforms

## Platform-Specific Considerations

### macOS
- Use `is_macos()` to detect macOS platform
- Use `<Meta>` key for Command key shortcuts

### Linux
- Use `<primary>` key for Ctrl key shortcuts
- Prefer system package managers for GTK dependencies

## Security Guidelines

- Never store passwords in plain text
- Use `secretstorage` on Linux for credential storage
- Use `keyring` for cross-platform credential management

## Build and Packaging


### Flatpak
- Use `io.github.mfat.sshpilot.yaml` manifest
- Never use shell commands to generate scripts in manifest
- Use `type: script` or include files directly

### macOS DMG
- Use version from `__init__.py` for DMG naming

## Git and Release Guidelines
- Project tags follow format `vX.Y.Z` (e.g., `v2.7.1`)

## Common Patterns

### SSH Configuration
- The project uses 2 operation modes: default (loads and saves ~/.ssh/config) abd Isolated Mode which stores config in ~/.config/sshpilot

### Terminal Management
- Use VTE for terminal display
- Supports both built-in terminal and external terminal options

## Debugging
- Use `--verbose` flag for detailed logging


## Memory and Preferences
- Do not add/remove features without user's confirmation
- User prefers no UI files; define all interfaces in code
- User requires explicit permission before modifying/deploying codebase