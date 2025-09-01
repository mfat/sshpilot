# PyGObject macOS App Bundling Guide

*Based on real-world experience bundling sshPilot with gtk-mac-bundler*

## Overview

This guide documents the process of creating a macOS app bundle for a PyGObject application using `gtk-mac-bundler`. We'll cover the challenges we faced, solutions we implemented, and best practices we discovered.

## Prerequisites

- macOS 10.15+ (Catalina or later)
- Homebrew installed
- Python 3.x with PyGObject bindings
- GTK4 stack installed via Homebrew

## Architecture Overview

```
sshPilot.app/
├── Contents/
│   ├── Info.plist          # App metadata and executable reference
│   ├── MacOS/
│   │   └── sshPilot        # Main executable (Python launcher)
│   └── Resources/
│       ├── sshPilot.icns   # App icon
│       ├── sshPilot/       # Python application code
│       ├── run.py          # Main entry point
│       └── share/icons/    # System icon themes
```

## Key Components

### 1. Bundle Recipe (`io.github.mfat.sshpilot.bundle`)

The XML configuration file that tells `gtk-mac-bundler` how to structure the app bundle:

```xml
<?xml version="1.0" encoding="utf-8"?>
<app-bundle>
  <meta>
    <prefix name="default">${env:BREW_PREFIX}</prefix>
    <destination overwrite="yes">${env:HOME}/Desktop</destination>
    <run-install-name-tool/>
    <gtk>gtk4</gtk>
  </meta>

  <plist>${project}/Info.plist</plist>

  <main-binary dest="${bundle}/Contents/MacOS/sshPilot">
    ${env:BUILD_DIR}/sshPilot-launcher-main.py
  </main-binary>

  <!-- Copy application files -->
  <data dest="${bundle}/Contents/Resources/sshpilot">
    ${env:BUILD_DIR}/sshpilot/
  </data>

  <!-- Copy resources -->
  <data dest="${bundle}/Contents/Resources">
    ${env:BUILD_DIR}/run.py
  </data>

  <!-- Copy app icon -->
  <data dest="${bundle}/Contents/Resources">
    ${env:BUILD_DIR}/sshPilot.icns
  </data>
</app-bundle>
```

**Key Points:**
- Use `<app-bundle>` root element (not `<application>`)
- Include `<plist>` tag referencing Info.plist
- Use `${env:VARIABLE}` for dynamic paths
- Set proper `dest` attributes for all data elements

### 2. Python Launcher (`sshPilot-launcher-main.py`)

**Why Python instead of shell script?**
- macOS security restrictions often reject shell script executables
- Python launcher provides better error handling and debugging
- More reliable path resolution and environment setup

```python
#!/usr/bin/env python3
import os
import sys

def setup_environment():
    """Set up environment variables for GTK/PyGObject"""
    
    # Get bundle paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_contents = os.path.join(script_dir, '..', '..')
    bundle_res = os.path.join(bundle_contents, 'Resources')
    
    # Set environment variables
    env_vars = {
        'XDG_DATA_DIRS': f'/usr/local/share:{bundle_res}/share',
        'GTK_DATA_PREFIX': bundle_res,
        'DYLD_FALLBACK_LIBRARY_PATH': f'/usr/local/lib:{os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")}',
        'GI_TYPELIB_PATH': f'/usr/local/lib/girepository-1.0:{os.environ.get("GI_TYPELIB_PATH", "")}',
        'GTK_ICON_THEME': 'Adwaita',
        'GTK_THEME': '',  # Allow automatic theme switching
    }
    
    for key, value in env_vars.items():
        os.environ[key] = value

def main():
    setup_environment()
    
    # Add Resources to Python path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_res = os.path.join(script_dir, '..', 'Resources')
    sys.path.insert(0, bundle_res)
    
    # Change to Resources directory and run app
    os.chdir(bundle_res)
    from run import main as app_main
    app_main()

if __name__ == "__main__":
    main()
```

### 3. Info.plist

Essential metadata for macOS to recognize the app bundle:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>sshPilot</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.mfat.sshpilot</string>
    <key>CFBundleName</key>
    <string>sshPilot</string>
    <key>CFBundleVersion</key>
    <string>2.7.1</string>
    <key>CFBundleIconFile</key>
    <string>sshPilot.icns</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSRequiresAquaSystemAppearance</key>
    <false/>
</dict>
</plist>
```

**Critical Keys:**
- `CFBundleExecutable`: Must match the actual executable name in MacOS/
- `CFBundleIconFile`: References the .icns file in Resources/
- `LSMinimumSystemVersion`: Set to 10.15+ for GTK4 compatibility

### 4. App Icon Creation

macOS requires `.icns` files with multiple resolutions:

```bash
# Create iconset directory
mkdir -p sshPilot.iconset

# Generate required sizes
sips -z 16 16 icon.png --out sshPilot.iconset/icon_16x16.png
sips -z 32 32 icon.png --out sshPilot.iconset/icon_16x16@2x.png
sips -z 32 32 icon.png --out sshPilot.iconset/icon_32x32.png
sips -z 64 64 icon.png --out sshPilot.iconset/icon_32x32@2x.png
sips -z 128 128 icon.png --out sshPilot.iconset/icon_128x128.png
sips -z 256 256 icon.png --out sshPilot.iconset/icon_128x128@2x.png
sips -z 512 512 icon.png --out sshPilot.iconset/icon_256x256@2x.png

# Convert to icns
iconutil -c icns sshPilot.iconset -o sshPilot.icns
```

## Build Process

### 1. Setup Script (`gtk-osx-setup.sh`)

```bash
#!/bin/bash
set -euo pipefail

# Install gtk-mac-bundler from source
GTK_MAC_BUNDLER_DIR="${HOME}/gtk-mac-bundler"

if [ ! -d "${GTK_MAC_BUNDLER_DIR}" ]; then
    git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git "${GTK_MAC_BUNDLER_DIR}"
    pushd "${GTK_MAC_BUNDLER_DIR}" >/dev/null
    make install
    popd >/dev/null
fi

# Ensure Homebrew GTK stack is available
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection
```

### 2. Build Script (`make-bundle.sh`)

```bash
#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build/macos-bundle"
DIST_DIR="${ROOT_DIR}/dist"

# Create build directory
mkdir -p "${BUILD_DIR}"

# Copy application files
cp -R "${ROOT_DIR}/sshpilot" "${BUILD_DIR}/"
cp "${ROOT_DIR}/run.py" "${BUILD_DIR}/"
cp "${SCRIPT_DIR}/sshPilot-launcher-main.py" "${BUILD_DIR}/"
cp "${SCRIPT_DIR}/sshPilot.icns" "${BUILD_DIR}/"
cp "${SCRIPT_DIR}/Info.plist" "${BUILD_DIR}/"

# Set environment variables for gtk-mac-bundler
export BUILD_DIR="${BUILD_DIR}"
export BREW_PREFIX="$(brew --prefix)"

# Create app bundle
gtk-mac-bundler "${SCRIPT_DIR}/io.github.mfat.sshpilot.bundle"

# Move to dist and sign
mv "${HOME}/Desktop/sshPilot.app" "${DIST_DIR}/"
codesign --force --deep --sign - "${DIST_DIR}/sshPilot.app"
```

## Common Issues and Solutions

### 1. "The file has no <main-binary> tag"

**Problem**: Bundle recipe missing required `<main-binary>` tag
**Solution**: Always include `<main-binary>` with proper `dest` attribute

### 2. "Cannot find main binary"

**Problem**: Path resolution issues in bundle recipe
**Solution**: Use `${env:BUILD_DIR}` for absolute paths, not `${project}`

### 3. "ValueError: The 'plist' tag is required"

**Problem**: Missing Info.plist reference
**Solution**: Include `<plist>${project}/Info.plist</plist>` in bundle recipe

### 4. Missing Icons

**Problem**: App bundle has no icon
**Solution**: 
- Create proper .icns file with multiple resolutions
- Add `CFBundleIconFile` to Info.plist
- Include icon in bundle recipe

### 5. Double-Click Not Working

**Problem**: macOS Gatekeeper rejects unsigned app bundles
**Solution**: 
- Use `open sshPilot.app` command line
- Right-click → "Open" → "Open" in security dialog
- System Preferences → Security & Privacy → "Open Anyway"

### 6. GTK Libraries Not Found

**Problem**: App can't find GTK libraries
**Solution**: 
- Use system Homebrew GTK stack instead of bundling
- Set `DYLD_FALLBACK_LIBRARY_PATH="/usr/local/lib"`
- Set `GI_TYPELIB_PATH="/usr/local/lib/girepository-1.0"`

### 7. Theme Switching Not Working

**Problem**: App stuck in one theme
**Solution**:
- Don't hardcode `GTK_THEME`
- Set `GTK_ICON_THEME="Adwaita"`
- Include `/usr/local/share` in `XDG_DATA_DIRS`

## Environment Variables Reference

```bash
# Library paths
export DYLD_FALLBACK_LIBRARY_PATH="/usr/local/lib:$DYLD_FALLBACK_LIBRARY_PATH"
export GI_TYPELIB_PATH="/usr/local/lib/girepository-1.0:$GI_TYPELIB_PATH"

# Data directories
export XDG_DATA_DIRS="/usr/local/share:$bundle_data"
export GTK_DATA_PREFIX="$bundle_res"
export GTK_EXE_PREFIX="$bundle_res"

# Icon and theme
export GTK_ICON_THEME="Adwaita"
export GTK_THEME=""  # Allow automatic switching
export GTK_USE_PORTAL="1"
export GTK_CSD="1"
```

## Testing and Debugging

### 1. Test Launcher Directly

```bash
cd dist/sshPilot.app/Contents/MacOS
./sshPilot
```

### 2. Check Bundle Structure

```bash
# Verify executable
ls -la dist/sshPilot.app/Contents/MacOS/

# Check resources
ls -la dist/sshPilot.app/Contents/Resources/

# Validate Info.plist
plutil -p dist/sshPilot.app/Contents/Info.plist
```

### 3. Debug Environment Variables

```bash
# Add debug output to launcher
export GTK_DEBUG_LAUNCHER=1
./sshPilot
```

### 4. Check Code Signing

```bash
codesign --verify --verbose dist/sshPilot.app
spctl --assess --type execute --verbose dist/sshPilot.app
```

## Best Practices

1. **Use Python Launcher**: More reliable than shell scripts
2. **Don't Bundle GTK Libraries**: Use system Homebrew stack
3. **Proper Icon Sizes**: Include all required macOS icon resolutions
4. **Environment Variables**: Set all necessary GTK/PyGObject paths
5. **Error Handling**: Add proper error handling in launcher
6. **Documentation**: Document launch methods for users
7. **Code Signing**: Always sign the bundle (even ad-hoc)

## Complete Working Example

The sshPilot project demonstrates all these concepts working together:

- ✅ Proper bundle structure with gtk-mac-bundler
- ✅ Python-based launcher with environment setup
- ✅ System GTK stack integration
- ✅ Icon and theme support
- ✅ Code signing and security handling
- ✅ Comprehensive documentation

## Resources

- [PyGObject Deployment Guide](https://pygobject.gnome.org/guide/deploy.html)
- [gtk-mac-bundler Documentation](https://gitlab.gnome.org/GNOME/gtk-mac-bundler)
- [macOS App Bundle Structure](https://developer.apple.com/library/archive/documentation/CoreFoundation/Conceptual/CFBundles/BundleTypes/BundleTypes.html)
- [Homebrew GTK Installation](https://formulae.brew.sh/formula/gtk4)

---

*This guide is based on real-world experience bundling sshPilot. The solutions presented here address actual challenges we encountered and resolved.*
