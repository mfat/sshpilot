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
./build-final.sh
```

