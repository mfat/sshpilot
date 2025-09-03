# PyGObject macOS Bundling - Quick Reference

## 🚀 Quick Start

```bash
# 1. Build app bundle (sets up dependencies automatically)
bash packaging/macos/make-bundle.sh

# 2. Launch app
open dist/sshPilot.app
```

## 📁 Required Files

- `io.github.mfat.sshpilot.bundle` - Bundle recipe (XML)
- `sshPilot-launcher-main.py` - Python launcher
- `Info.plist` - App metadata
- `sshPilot.icns` - App icon
- `make-bundle.sh` - Build script

## 🔧 Key Commands

```bash
# Install gtk-mac-bundler
git clone https://gitlab.gnome.org/GNOME/gtk-mac-bundler.git
cd gtk-mac-bundler && make install

# Create app icon
iconutil -c icns sshPilot.iconset -o sshPilot.icns

# Sign bundle
codesign --force --deep --sign - sshPilot.app

# Test bundle
spctl --assess --type execute --verbose sshPilot.app
```

## ⚠️ Common Issues

| Issue | Solution |
|-------|----------|
| "No <main-binary> tag" | Add `<main-binary>` to bundle recipe |
| "Cannot find main binary" | Use `${env:BUILD_DIR}` for paths |
| "The 'plist' tag is required" | Add `<plist>` reference |
| Double-click not working | Use `open sshPilot.app` command |
| Missing icons | Create .icns file + CFBundleIconFile |
| GTK libraries not found | Set `DYLD_FALLBACK_LIBRARY_PATH` |
| **PyGObject circular imports** | **Use subprocess in launcher** |

## 🚨 CRITICAL: PyGObject Circular Imports

**NEVER import PyGObject directly in the launcher!**

```python
# ❌ WRONG - Causes circular imports
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

# ✅ CORRECT - Use subprocess
subprocess.run([python_executable, 'run.py'], env=os.environ)
```

**Why?** PyGObject has complex initialization that causes `cannot import name '_gi'` errors when imported in the launcher.

## 🌟 Pro Tips

1. **Use Python launcher** instead of shell scripts
2. **Use subprocess** to avoid PyGObject circular imports
3. **Don't bundle GTK libraries** - use system Homebrew stack
4. **Always sign the bundle** (even ad-hoc)
5. **Test launcher directly** before bundling
6. **Set all environment variables** for GTK/PyGObject

## 📚 Full Documentation

See `PYGOBJECT_MACOS_BUNDLING_GUIDE.md` for complete details.
