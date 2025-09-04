# sshPilot macOS Installation Guide

This guide explains how to build and install sshPilot on macOS from source.

## Prerequisites

### System Requirements
- macOS 10.15 (Catalina) or later
- Xcode Command Line Tools
- Homebrew package manager

### Install Prerequisites

1. **Install Xcode Command Line Tools:**
   ```bash
   xcode-select --install
   ```

2. **Install Homebrew (if not already installed):**
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

3. **Install Required Dependencies:**
   ```bash
   brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass
   ```

## Building sshPilot

This method creates a working app bundle using system Homebrew packages:

```bash
# Clone the repository
git clone https://github.com/mfat/sshpilot.git
cd sshpilot

# Build the app bundle
cd packaging/macos
./build-final.sh
```

The built app will be available at `packaging/macos/build/sshPilot.app`

## Installation

After building, you can:

1. **Run directly**: Double-click `sshPilot.app` in Finder
2. **Install system-wide**: Copy `sshPilot.app` to `/Applications/`
3. **Test the bundle**: Run `./test-bundle.sh` to verify functionality

## Troubleshooting

### Common Issues

1. **"App can't be opened because it is from an unidentified developer"**
   ```bash
   xattr -rd com.apple.quarantine sshPilot.app
   ```

2. **Missing dependencies**
   - Ensure all Homebrew packages are installed
   - Run `brew doctor` to check for issues

3. **Build failures**
   - Check that Xcode Command Line Tools are installed
   - Verify Homebrew is up to date: `brew update && brew upgrade`

### Getting Help

- Check the [GitHub Issues](https://github.com/mfat/sshpilot/issues)
- Review the build logs for specific error messages
- Ensure you're using a supported macOS version (10.15+)

