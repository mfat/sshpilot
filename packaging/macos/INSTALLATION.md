# sshPilot macOS Installation Guide

This guide explains how to build and install sshPilot on macOS using the official PyGObject deployment methods.

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
   
   # Install gtk-mac-bundler manually (not available in Homebrew)
   git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git /tmp/gtk-mac-bundler
   cd /tmp/gtk-mac-bundler && make install
   ```

## Building sshPilot

### Method 1: Simple Build (Recommended for Development)

This method creates a quick build that uses system Homebrew packages:

```bash
cd packaging/macos
./build.sh simple
```

**Advantages:**
- Fast build time (5-10 minutes)
- Easy to set up
- Good for development and testing

**Requirements:**
- Target system must have Homebrew packages installed

### Method 2: Standalone Build (Recommended for Distribution)

This method creates a self-contained bundle with all dependencies:

```bash
cd packaging/macos
./build.sh standalone
```

**Advantages:**
- Self-contained bundle
- No external dependencies required
- Good for distribution

**Requirements:**
- Larger bundle size (~500MB)
- Longer build time (15-30 minutes)

### Method 3: gtk-osx Build (Maximum Compatibility)

This method follows the official PyGObject deployment guide:

```bash
cd packaging/macos
./build.sh setup    # First time only
./build.sh gtk-osx  # Full build (takes hours)
```

**Advantages:**
- Maximum compatibility
- Follows official deployment guide
- Most reliable for distribution

**Requirements:**
- Very long build time (2-4 hours)
- Complex setup process
- Requires significant disk space (~2GB)

## Testing the Build

After building, test the app bundle:

```bash
./build.sh test
```

This will verify:
- All dependencies are properly bundled
- Python packages are available
- GTK modules can be imported
- Application can launch

## Installation

### Option 1: Direct Installation

1. Copy the built app bundle to Applications:
   ```bash
   cp -r build/sshPilot.app /Applications/
   ```

2. Launch from Applications folder or Spotlight

### Option 2: Create DMG Installer

1. Create a DMG file:
   ```bash
   hdiutil create -volname "sshPilot" -srcfolder build/sshPilot.app -ov -format UDZO sshPilot.dmg
   ```

2. Distribute the DMG file

## Troubleshooting

### Common Issues

1. **"gtk-mac-bundler not found"**
   ```bash
   brew install gtk-mac-bundler
   ```

2. **Missing Homebrew packages**
   ```bash
   brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass
   ```

3. **Permission denied errors**
   ```bash
   chmod +x *.sh
   ```

4. **Python import errors**
   - Ensure all packages from requirements.txt are installed
   - Check that the virtual environment is properly set up

5. **Library not found errors**
   - For standalone builds, ensure library paths are correctly set
   - For simple builds, ensure Homebrew packages are installed

### Getting Help

1. Check the test output for specific missing dependencies
2. Verify Homebrew installation and package availability
3. Ensure sufficient disk space for the build process
4. Check macOS version compatibility

## File Structure

After building, the app bundle structure will be:

```
sshPilot.app/
├── Contents/
│   ├── Info.plist          # App metadata
│   ├── MacOS/
│   │   └── sshPilot        # Main executable
│   └── Resources/
│       ├── sshpilot.icns   # App icon
│       ├── venv/           # Python virtual environment
│       ├── lib/            # System libraries (standalone build)
│       ├── bin/            # System binaries
│       └── share/          # Shared data files
```

## Distribution

### Code Signing (Optional)

For distribution outside the App Store:

```bash
# Sign the app bundle
codesign --force --deep --sign "Developer ID Application: Your Name" sshPilot.app

# Verify signing
codesign --verify --verbose sshPilot.app
```

### Notarization (Optional)

For macOS Gatekeeper compatibility:

```bash
# Create zip for notarization
ditto -c -k --keepParent sshPilot.app sshPilot.zip

# Submit for notarization
xcrun altool --notarize-app --primary-bundle-id "io.github.mfat.sshpilot" --username "your-email@example.com" --password "@keychain:AC_PASSWORD" --file sshPilot.zip

# Staple notarization
xcrun stapler staple sshPilot.app
```

## Support

For issues with the build process:

1. Check the comprehensive README.md in this directory
2. Review the test output for specific error messages
3. Ensure all prerequisites are properly installed
4. Try different build methods if one fails
