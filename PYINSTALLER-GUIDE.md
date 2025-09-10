# Building SSHPilot for macOS with PyInstaller

This document explains how to build SSHPilot as a standalone macOS application bundle using PyInstaller.

## Prerequisites

- macOS with Homebrew installed
- Python 3.13+ (via Homebrew)
- GTK 4 and related libraries (via Homebrew)
- PyInstaller (installed in the virtual environment)

## Quick Build

To build the application bundle, simply run:

```bash
./pyinstaller.sh
```

This script will:
1. ✅ Activate the Homebrew virtual environment
2. ✅ Run PyInstaller with the correct configuration
3. ✅ Apply code signing to the bundle
4. ✅ Provide success confirmation and location

## Manual Build

If you prefer to build manually:

```bash
# Activate the virtual environment
source .venv-homebrew/bin/activate

# Build the bundle
python -m PyInstaller --clean --noconfirm sshpilot.spec

# Apply code signing
codesign --force --deep --sign - ./dist/SSHPilot.app
```

## Output

The build process creates:
- `dist/SSHPilot.app` - The standalone macOS application bundle
- `build/` - Temporary build files (can be deleted)

## Running the Application

After building, you can run the application by:

```bash
# From command line
open dist/SSHPilot.app

# Or double-click the app in Finder
```

## Troubleshooting

### Build Fails with Library Errors
- Ensure you're using the Homebrew virtual environment (`.venv-homebrew`)
- Verify all GTK libraries are installed via Homebrew
- Check that the `sshpilot.spec` file is in the project root

### Application Won't Launch
- Ensure code signing was applied successfully
- Check that all required libraries are bundled in `dist/SSHPilot.app/Contents/Frameworks/`

### Cairo Context Errors
- This should be resolved with the current configuration
- If issues persist, ensure the correct Python environment is being used

## Configuration Files

- `sshpilot.spec` - PyInstaller configuration
- `hook-gtk_runtime.py` - Runtime hook for GTK environment setup
- `build-bundle.sh` - Automated build script

## Notes

- The bundle is built with ad-hoc code signing (`-`) for development
- For distribution, you may want to use a proper Apple Developer certificate
- The application hides external terminal options on macOS unless a third-party terminal is available
