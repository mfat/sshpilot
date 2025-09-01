# macOS Packaging for sshPilot

This directory contains everything needed to create a professional macOS application bundle and DMG installer for sshPilot.

## 🚀 Quick Start

### Prerequisites
- macOS 10.15 (Catalina) or later
- Homebrew installed
- Python 3.13+

### Install Dependencies
```bash
# Install GTK4 and dependencies
brew install gtk4 libadwaita pygobject3

# Install gtk-mac-bundler from source
git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git
cd gtk-mac-bundler
make install
cd ..
rm -rf gtk-mac-bundler

# Install DMG creation tool
brew install create-dmg

# Install Python dependencies (use virtual environment for Python 3.13+)
python3 -m venv venv
source venv/bin/activate
pip install -r ../../requirements.txt
deactivate
```

### Build Everything
```bash
# Activate virtual environment
source venv/bin/activate

# Build the app bundle
bash make-bundle.sh

# Create the DMG installer
bash create-dmg.sh

# Deactivate virtual environment
deactivate
```

## 📁 What Gets Created

- **`dist/sshPilot.app`** - Fully self-contained macOS application bundle
- **`dist/sshPilot-macOS.dmg`** - Professional installer package

## 🔧 Manual Build Process

### 1. Build App Bundle
```bash
# Activate virtual environment first
source venv/bin/activate

bash make-bundle.sh

# Deactivate when done
deactivate
```

This script:
- Installs Python dependencies
- Copies application files
- Uses `gtk-mac-bundler` to create the `.app` bundle
- Signs the bundle for macOS compatibility
- Creates a fully self-contained application

### 2. Create DMG Installer
```bash
bash create-dmg.sh
```

This script:
- Creates a professional DMG layout
- Includes the app bundle and Applications shortcut
- Uses your custom icon
- Creates a clean, professional installer

## 🤖 Automated Builds with GitHub Actions

### Automatic Release Builds
The main workflow (`build-macos.yml`) automatically triggers on version tags:

```bash
# Create a new release
git tag v2.7.1
git push origin v2.7.1
```

This will:
- Build the macOS bundle
- Create the DMG installer
- Test the application
- Create a GitHub release with the DMG
- Upload artifacts for download

### Manual Builds
You can manually trigger builds from the GitHub Actions tab:
1. Go to Actions → Build macOS Bundle and DMG
2. Click "Run workflow"
3. Enter the version (e.g., `v2.7.1`)
4. Click "Run workflow"

### Test Builds
The test workflow (`test-build.yml`) runs on:
- Every push to `main` and `develop` branches
- Pull requests to `main`
- Manual triggers

This ensures your builds work before creating releases.

## 🎯 Key Features

### Fully Self-Contained
- ✅ No external dependencies required
- ✅ Bundled GTK libraries and Python packages
- ✅ Works on any macOS 10.15+ system
- ✅ Professional appearance and behavior

### Double-Click Launch
- ✅ Works from Finder
- ✅ No command line required
- ✅ Native macOS experience
- ✅ Proper code signing

### Professional Distribution
- ✅ Clean DMG installer
- ✅ Applications folder shortcut
- ✅ Custom app icon
- ✅ Professional branding

## 🔍 Troubleshooting

### Common Issues

**"gtk-mac-bundler not found"**
```bash
# Install from source (not available in Homebrew)
git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git
cd gtk-mac-bundler
make install
cd ..
rm -rf gtk-mac-bundler
```

**"create-dmg not found"**
```bash
brew install create-dmg
```

**"externally-managed-environment" error (Python 3.13+)**
```bash
# Use virtual environment instead of system pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
```

**App doesn't launch with double-click**
- Ensure the bundle is properly signed
- Check that all environment variables are set correctly
- Verify the launcher script has proper permissions

**GTK libraries not found**
- Ensure Homebrew GTK stack is installed
- Check `DYLD_FALLBACK_LIBRARY_PATH` is set
- Verify `GI_TYPELIB_PATH` points to correct location

### Debug Mode
Add debug output to the launcher:
```bash
export GTK_DEBUG_LAUNCHER=1
./sshPilot.app/Contents/MacOS/sshPilot
```

## 📚 Documentation

- **`PYGOBJECT_MACOS_BUNDLING_GUIDE.md`** - Comprehensive bundling guide
- **`QUICK_REFERENCE.md`** - Quick reference for developers
- **`Info.plist`** - macOS application metadata
- **`sshPilot.icns`** - Application icon

## 🎉 Success Indicators

Your build is successful when:
- ✅ `sshPilot.app` launches with double-click
- ✅ All SSH functionality works
- ✅ DMG mounts and shows proper layout
- ✅ App can be dragged to Applications
- ✅ No external dependencies required

## 🚀 Distribution

Once built, your users can:
1. Download the DMG
2. Double-click to mount
3. Drag the app to Applications
4. Launch immediately

No installation, no dependencies, no setup required!
