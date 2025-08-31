## macOS Installation Guide

This guide provides simple step-by-step instructions to install and run sshPilot on macOS.

### Prerequisites

- macOS 10.15 (Catalina) or later
- Homebrew (for system dependencies)
- Python 3.8 or later

### Quick Start (Recommended)

Use the launcher script for automatic setup:

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
chmod +x sshpilot-mac.sh
./sshpilot-mac.sh
```

The launcher handles all dependencies and launches the app automatically.

---

### Manual Installation

Choose one of the two installation methods below:

#### Option A: System-wide Installation (Simpler)

Install sshPilot system-wide without virtual environments.

**Step 1: Install Homebrew (if not installed)**
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**Step 2: Install system dependencies**
```bash
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass
```

**Step 3: Clone and install**
```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
pip3 install -r requirements.txt
```

**Step 4: Set up environment and run**
```bash
export PATH="$(brew --prefix)/bin:$PATH"
export GI_TYPELIB_PATH="$(brew --prefix)/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$(brew --prefix)/lib:$DYLD_LIBRARY_PATH"
python3 run.py
```

---

#### Option B: Virtual Environment Installation (Recommended for development)

Install sshPilot in a virtual environment for isolation.

**Step 1: Install Homebrew (if not installed)**
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**Step 2: Install system dependencies**
```bash
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass
```

**Step 3: Clone repository**
```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
```

**Step 4: Create and activate virtual environment**
```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
```

**Step 5: Install Python dependencies**
```bash
pip install -r requirements.txt
```

**Step 6: Set up environment and run**
```bash
export PATH="$(brew --prefix)/bin:$PATH"
export GI_TYPELIB_PATH="$(brew --prefix)/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$(brew --prefix)/lib:$DYLD_LIBRARY_PATH"
python run.py
```

---

### Running the Application

**First time setup:**
```bash
# Option A (system-wide)
python3 run.py

# Option B (virtual environment)
source .venv/bin/activate
python run.py
```

**Subsequent runs:**
```bash
# Option A (system-wide)
cd sshpilot
export PATH="$(brew --prefix)/bin:$PATH"
export GI_TYPELIB_PATH="$(brew --prefix)/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$(brew --prefix)/lib:$DYLD_LIBRARY_PATH"
python3 run.py

# Option B (virtual environment)
cd sshpilot
source .venv/bin/activate
export PATH="$(brew --prefix)/bin:$PATH"
export GI_TYPELIB_PATH="$(brew --prefix)/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$(brew --prefix)/lib:$DYLD_LIBRARY_PATH"
python run.py
```

### Password Storage

- SSH passphrases and connection passwords are stored securely in macOS Keychain
- The `keyring` package (installed via pip) handles this automatically
- On first use, macOS may prompt for Keychain access - this is normal

### Troubleshooting

**"sshpass not found"**
```bash
brew install sshpass
export PATH="$(brew --prefix)/bin:$PATH"
```

**"Namespace not available" errors**
```bash
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme
export GI_TYPELIB_PATH="$(brew --prefix)/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$(brew --prefix)/lib:$DYLD_LIBRARY_PATH"
```

**Keyring/keychain access issues**
```bash
# Test keyring installation
python -c "import keyring; print('Keyring OK')"

# Verify macOS Keychain backend
python -c "import keyring; print(f'Backend: {keyring.get_keyring().__class__.__name__}')"
# Should output: Backend: Keyring
```

**Permission denied errors**
```bash
chmod +x sshpilot-mac.sh
chmod +x setup_dev_env.sh
```

### Development

For development work, use Option B (virtual environment) and run:
```bash
./setup_dev_env.sh
```

This sets up the development environment with all necessary dependencies and environment variables.



