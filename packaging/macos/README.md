# macOS App Bundle Packaging

This directory contains scripts and configuration files for creating a redistributable macOS app bundle for sshPilot, following the official PyGObject deployment guide.

## Overview

We provide multiple build methods to suit different needs:

1. **Simple Build** - Quick development build using Homebrew packages
2. **Standalone Build** - Self-contained bundle with all dependencies copied
3. **gtk-osx Build** - Full gtk-osx build for maximum compatibility

## Quick Start

```bash
# For development (requires Homebrew on target system)
./build.sh simple

# For production (self-contained bundle)
./build.sh standalone

# Test an existing bundle
./build.sh test
```

## Files

- `build.sh` - Main build script with multiple methods
- `build-bundle-simple.sh` - Simple build using Homebrew packages
- `build-bundle-standalone.sh` - Standalone build with copied dependencies
- `build-bundle.sh` - Full gtk-osx build script
- `setup-gtk-osx.sh` - Setup script for gtk-osx environment
- `test-bundle.sh` - Comprehensive test script
- `sshpilot.icns` - App icon in macOS format
- `Info.plist` - App bundle metadata
- `jhbuildrc` - jhbuild configuration for gtk-osx
- `bundle.ini` - gtk-mac-bundler configuration

## Build Methods

### 1. Simple Build (Recommended for Development)

```bash
./build.sh simple
```

**Pros:**
- Fast build time (minutes)
- Easy to set up
- Good for development and testing

**Cons:**
- Requires Homebrew packages on target system
- Not truly self-contained

### 2. Standalone Build (Recommended for Distribution)

```bash
./build.sh standalone
```

**Pros:**
- Self-contained bundle
- No external dependencies required
- Good for distribution

**Cons:**
- Larger bundle size
- Longer build time
- May have compatibility issues

### 3. gtk-osx Build (Maximum Compatibility)

```bash
# First time setup
./build.sh setup

# Build (takes several hours)
./build.sh gtk-osx
```

**Pros:**
- Maximum compatibility
- Follows official PyGObject deployment guide
- Most reliable for distribution

**Cons:**
- Very long build time (hours)
- Complex setup process
- Requires significant disk space

## Dependencies

The bundle includes all required dependencies:

### Python Packages (from requirements.txt)
- PyGObject>=3.42
- pycairo>=1.20.0
- paramiko>=3.4
- cryptography>=42.0
- keyring>=24.3
- psutil>=5.9.0

### System Dependencies
- GTK4, libadwaita, PyGObject, pycairo
- VTE3 for terminal functionality
- gobject-introspection, adwaita-icon-theme
- sshpass for SSH password authentication
- glib, graphene, icu4c, pkg-config

## Prerequisites

### For Simple/Standalone Builds
```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install required packages
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass

# Install gtk-mac-bundler manually (not available in Homebrew)
git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git /tmp/gtk-mac-bundler
cd /tmp/gtk-mac-bundler && make install
```

### For gtk-osx Build
```bash
# Run the setup script
./build.sh setup

# Follow the instructions to complete jhbuild setup
```

## Testing

The test script verifies that all dependencies are properly bundled:

```bash
# Test a specific bundle
./build.sh test [path-to-bundle]

# Test the default build location
./build.sh test
```

The test checks:
- Bundle structure and executable
- Python environment and packages
- GTK and GI modules
- Application modules
- System binaries
- Resources and icons
- Info.plist configuration
- Application launch capability

## Distribution

After building, you can:

1. **Direct Distribution**: Share the `sshPilot.app` bundle directly
2. **DMG Creation**: Use `hdiutil` to create a DMG installer
3. **Code Signing**: Sign the bundle for distribution outside the App Store
4. **Notarization**: Notarize the bundle for macOS Gatekeeper

### Creating a DMG

```bash
# Create a DMG from the app bundle
hdiutil create -volname "sshPilot" -srcfolder build/sshPilot.app -ov -format UDZO sshPilot.dmg
```

## Troubleshooting

### Common Issues

1. **Missing Dependencies**: Ensure all Homebrew packages are installed
2. **Permission Errors**: Make sure scripts are executable (`chmod +x`)
3. **Library Path Issues**: The standalone build automatically fixes library paths
4. **Python Import Errors**: Check that all Python packages are installed in the virtual environment

### Getting Help

- Check the test output for specific missing dependencies
- Verify Homebrew installation and package availability
- Ensure you have sufficient disk space for the build process
- Check macOS version compatibility (requires macOS 10.15+)
