# How to Run sshPilot.app on macOS

## The Issue
Due to macOS security requirements (Gatekeeper), unsigned app bundles cannot be launched by double-clicking in Finder.

## How to Run the App

### Method 1: Command Line (Recommended)
```bash
open sshPilot.app
```

### Method 2: Right-Click Method
1. Right-click on `sshPilot.app` in Finder
2. Select "Open" from the context menu
3. Click "Open" in the security dialog that appears

### Method 3: Security Settings
1. Go to System Preferences > Security & Privacy > General
2. Look for a message about "sshPilot.app was blocked..."
3. Click "Open Anyway"

## Why This Happens
macOS requires app bundles to be properly code signed to allow double-click launching. Since this is a development build, it uses an ad-hoc signature which doesn't meet the full security requirements.

## The App Bundle Works Perfectly
Once launched using any of the methods above, the app bundle works perfectly with:
- ✅ Full functionality (SSH connections, terminal, etc.)
- ✅ Proper icons and theme switching
- ✅ Standard macOS app behavior
- ✅ All GTK4/PyGObject features

The only limitation is the initial launch method due to macOS security requirements.
