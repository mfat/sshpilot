# macOS Build Instructions for sshPilot

This directory contains scripts and configurations to build a universal DMG for macOS that works on both Intel and Apple Silicon architectures.

## Quick Start

```bash
# Install build dependencies (requires Homebrew)
make install

# Build everything (clean, dependencies, app, DMG)
make all

# Or step by step:
make clean
make deps
make build
make dmg
make verify
```

## Requirements

- **macOS 11.0 or later**
- **Python 3.9 or later**
- **Homebrew** (for installing build tools)
- **Xcode Command Line Tools**

## Build Tools

The build system will automatically install:
- `create-dmg` - For creating styled DMG files
- `py2app` - For creating macOS app bundles
- Required Python packages

## Build Process

### 1. Dependencies Installation
```bash
make deps
```
Installs all Python dependencies listed in `macos_requirements.txt`.

### 2. App Bundle Creation
```bash
make build
```
Creates a universal app bundle using `py2app` with:
- Universal binary support (Intel x86_64 + Apple Silicon arm64)
- Proper macOS app structure
- Embedded Python runtime
- All required dependencies

### 3. DMG Creation
```bash
make dmg
```
Creates a styled DMG with:
- Custom background and layout
- Applications folder shortcut
- Proper volume icon
- Optimized compression

### 4. Verification
```bash
make verify
```
Verifies:
- DMG integrity
- Universal binary architecture
- File signatures

## Output

The build process creates:
- `dist/sshPilot.app` - Universal app bundle
- `dist/sshPilot-1.0.0-universal.dmg` - Final DMG file

## Architecture Support

The DMG includes a universal binary that supports:
- **Intel Macs** (x86_64) - All Intel-based Macs
- **Apple Silicon** (arm64) - M1, M1 Pro, M1 Max, M1 Ultra, M2, etc.

## Build Scripts

### Core Scripts
- `build_macos.py` - Main build orchestrator
- `build_universal.py` - Universal binary builder
- `create_styled_dmg.py` - DMG creation with styling
- `create_dmg.sh` - Shell script alternative
- `Makefile` - Build automation

### Configuration Files
- `macos_requirements.txt` - macOS-specific Python dependencies
- `setup.py` - Generated py2app configuration

## Troubleshooting

### Common Issues

1. **PyGObject not found**
   ```bash
   # Install GTK4 and PyGObject via Homebrew
   brew install gtk4 libadwaita pygobject3
   ```

2. **create-dmg fails**
   ```bash
   # The script will fallback to hdiutil
   # Or install create-dmg manually:
   brew install create-dmg
   ```

3. **Universal binary not created**
   - Ensure you're using Python 3.9+ with universal2 support
   - Check that py2app supports universal2 architecture

4. **App won't launch**
   - Verify all dependencies are included
   - Check that GResource files are properly bundled
   - Test on both Intel and Apple Silicon if possible

### Manual Build

If the automated scripts fail, you can build manually:

```bash
# 1. Install dependencies
pip3 install -r macos_requirements.txt

# 2. Create setup.py (see build_macos.py for template)
python3 -c "from build_macos import create_setup_py; create_setup_py()"

# 3. Build app
python3 setup.py py2app --arch=universal2

# 4. Create DMG
create-dmg --volname "sshPilot 1.0.0" \
           --window-size 600 400 \
           --icon-size 100 \
           --icon "sshPilot.app" 150 200 \
           --app-drop-link 450 200 \
           "dist/sshPilot-1.0.0-universal.dmg" \
           "dist/"
```

## Testing

To test the DMG:

1. **Mount the DMG**: Double-click the DMG file
2. **Install**: Drag sshPilot.app to Applications
3. **Launch**: Open from Applications folder or Spotlight
4. **Verify**: Check that all features work correctly

Test on both architectures if possible:
- Intel Mac or Rosetta 2 mode
- Apple Silicon native mode

## Distribution

The created DMG can be distributed and will work on:
- All Intel Macs running macOS 11.0+
- All Apple Silicon Macs running macOS 11.0+
- Future Apple architectures (through Rosetta compatibility)

## Code Signing (Optional)

For distribution outside the Mac App Store, consider code signing:

```bash
# Sign the app (requires Apple Developer account)
codesign --force --deep --sign "Developer ID Application: Your Name" dist/sshPilot.app

# Notarize (for Gatekeeper compatibility)
xcrun notarytool submit dist/sshPilot-1.0.0-universal.dmg \
  --apple-id your-apple-id@example.com \
  --team-id YOUR_TEAM_ID \
  --password your-app-password
```

## File Structure

```
workspace/
├── build_macos.py              # Main build script
├── build_universal.py          # Universal binary builder  
├── create_dmg.sh              # Shell DMG creator
├── create_styled_dmg.py       # Python DMG creator
├── macos_requirements.txt     # macOS dependencies
├── Makefile                   # Build automation
├── README_MACOS_BUILD.md      # This file
├── dist/                      # Build output
│   ├── sshPilot.app          # Universal app bundle
│   └── sshPilot-1.0.0-universal.dmg  # Final DMG
└── build/                     # Temporary build files
```