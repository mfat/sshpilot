# Building SSH Pilot for Windows

This guide explains how to build SSH Pilot for Windows from source.

## Prerequisites

### Required Software

1. **MSYS2** (https://www.msys2.org/)
   - Download and install MSYS2 (64-bit)
   - Default installation path: `C:\msys64`

2. **Python 3.12+** (via MSYS2)

3. **GTK4 and dependencies** (via MSYS2)

## Setup Development Environment

### 1. Install MSYS2

Download and install MSYS2 from https://www.msys2.org/

After installation, open **MSYS2 MinGW64** terminal and update the package database:

```bash
pacman -Syu
```

You may need to close and reopen the terminal, then run:

```bash
pacman -Su
```

### 2. Install Dependencies

In the **MSYS2 MinGW64** terminal, install the required packages:

```bash
# Python and development tools
pacman -S mingw-w64-x86_64-python \
          mingw-w64-x86_64-python-pip \
          mingw-w64-x86_64-python-gobject \
          mingw-w64-x86_64-python-cairo

# GTK4 and UI libraries
pacman -S mingw-w64-x86_64-gtk4 \
          mingw-w64-x86_64-libadwaita \
          mingw-w64-x86_64-gtksourceview5 \
          mingw-w64-x86_64-adwaita-icon-theme

# Build tools
pacman -S mingw-w64-x86_64-python-packaging \
          zip \
          git
```

### 3. Verify Installation

Check that Python and GTK4 are correctly installed:

```bash
python --version
python -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print(f'GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}')"
```

## Building SSH Pilot

### Option 1: Using the Build Script (Recommended)

1. Open **MSYS2 MinGW64** terminal
2. Navigate to the SSH Pilot source directory
3. Run the build script:

```bash
cd /c/path/to/sshpilot
./packaging/windows/build.bat
```

The script will:
- Create a virtual environment
- Install Python dependencies
- Build the application with PyInstaller
- Create a portable ZIP package (if 7-Zip is installed)

### Option 2: Manual Build

1. Create and activate a virtual environment:

```bash
python -m venv .venv-windows
source .venv-windows/Scripts/activate
```

2. Install Python packages:

```bash
pip install --upgrade pip wheel
pip install PyInstaller pyinstaller-hooks-contrib
pip install cryptography keyring psutil certifi pykeepass PyYAML
```

3. Build with PyInstaller:

```bash
export MSYS2_PREFIX=/mingw64
python -m PyInstaller --clean --noconfirm packaging/pyinstaller/sshpilot-windows.spec
```

4. The built application will be in `dist/SSHPilot/`

## Running the Application

After building, you can run SSH Pilot from the build directory:

```bash
cd dist/SSHPilot
./SSHPilot.exe
```

## Creating a Distribution Package

### Portable ZIP

Create a portable ZIP file that users can extract and run:

```bash
cd dist
7z a -tzip SSHPilot-Windows-portable.zip SSHPilot/
```

Or using PowerShell:

```powershell
Compress-Archive -Path "dist\SSHPilot\*" -DestinationPath "dist\SSHPilot-Windows-portable.zip"
```

### Installer (Optional)

You can create a Windows installer using:

- **NSIS** (Nullsoft Scriptable Install System)
- **WiX Toolset**
- **Inno Setup**

Example NSIS script would package the `dist/SSHPilot` directory into an installer.

## System Requirements

### Minimum Requirements

- **OS**: Windows 10 (version 1809 or later) or Windows 11
- **RAM**: 4 GB
- **Disk**: ~200 MB for installed application
- **Dependencies**: OpenSSH client (built-in on Windows 10+)

### Recommended

- Windows 10 21H2+ or Windows 11
- 8 GB RAM
- SSD storage

## Known Limitations on Windows

### Not Available

1. **VTE Terminal Widget**: The VTE (Virtual Terminal Emulator) is a Unix-only library. SSH Pilot on Windows uses alternative terminal backends.

2. **Native PTY Support**: Unix PTY (pseudo-terminal) APIs are not available on Windows. The daemon PTY runner is disabled.

3. **Unix File Permissions**: Advanced Unix file permission features are not fully supported in the file manager.

4. **sshpass**: The `sshpass` utility is Unix-only. SSH Pilot uses `SSH_ASKPASS` for password handling on Windows.

### Limited Functionality

1. **File Manager Permissions**: The SFTP file manager shows permissions but cannot modify them in the same way as on Unix.

2. **User/Group Names**: Windows doesn't have a direct equivalent to Unix pwd/grp, so user/group lookups use Windows alternatives.

## Troubleshooting

### Build Issues

**Problem**: `gi` module not found
```
Solution: Ensure you're using MSYS2 MinGW64 terminal and have installed mingw-w64-x86_64-python-gobject
```

**Problem**: GTK4 not found
```
Solution: Install GTK4: pacman -S mingw-w64-x86_64-gtk4 mingw-w64-x86_64-libadwaita
```

**Problem**: Missing DLLs when running
```
Solution: The build should bundle all DLLs. Check MSYS2_PREFIX is set correctly during build.
```

### Runtime Issues

**Problem**: Application doesn't start
```
Solution: 
1. Ensure OpenSSH is installed (check: ssh -V in cmd)
2. Check Windows Defender/antivirus isn't blocking it
3. Run from command prompt to see error messages
```

**Problem**: Terminals don't work
```
Solution: VTE terminals are not available on Windows. Use SSH connections which will use available terminal backends.
```

**Problem**: Can't save credentials
```
Solution: Ensure Windows Credential Manager is available. Check with: cmdkey /list
```

## Development Notes

### Path Handling

SSH Pilot uses `GLib` path functions which are cross-platform:
- `GLib.get_user_config_dir()` → `%APPDATA%\sshpilot`
- `GLib.get_user_data_dir()` → `%LOCALAPPDATA%\sshpilot`
- `GLib.get_home_dir()` → `%USERPROFILE%`

SSH configuration is at: `%USERPROFILE%\.ssh\config`

### Platform-Specific Code

The codebase includes Windows compatibility layers:
- `src/sshpilot/platform/windows_compat.py` - Unix module alternatives
- Platform checks use `platform_utils.is_windows()`
- VTE imports are guarded with try/except

### Testing on Windows

To test the application:

1. Build the application (see above)
2. Copy `dist/SSHPilot` to a clean Windows VM/system
3. Install OpenSSH for Windows if not present
4. Run `SSHPilot.exe`
5. Test basic operations:
   - Add SSH connection
   - Connect to a server
   - Test file manager (SFTP)
   - Test credential storage
   - Test key management

## Contributing

When contributing Windows-specific code:

1. Test on both Windows 10 and Windows 11
2. Use cross-platform APIs where possible (prefer GLib over os.path)
3. Add platform guards for Unix-only features
4. Update this documentation if adding new Windows-specific features
5. Test in both MSYS2 and standard Windows environments

## Additional Resources

- MSYS2 Documentation: https://www.msys2.org/docs/
- GTK4 on Windows: https://www.gtk.org/docs/installations/windows
- PyInstaller Manual: https://pyinstaller.org/en/stable/
- OpenSSH for Windows: https://docs.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse
