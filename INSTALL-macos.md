## macOS Installation and Run Guide

This guide shows three ways to run sshPilot on macOS (Apple Silicon or Intel):

### Option 1: Quick start with launcher script (Recommended)

Use the new launcher script that automatically handles everything:

```bash
# Make the launcher executable and run it
chmod +x sshpilot-mac.sh
./sshpilot-mac.sh
```

The launcher script will:
- Check and install required dependencies via Homebrew
- Set up environment variables automatically
- Test the setup and launch the application

### Option 2: Quick start with install script

Run the helper script to install dependencies, set up a venv, and launch the app. You can choose a target directory (defaults to `~/sshpilot`).

```bash
bash scripts/install-run-macos.sh "$HOME/sshpilot"
```

Next time, launch with:

```bash
bash scripts/run-macos.sh
```

### Option 3: Manual installation (venv-based)

The steps below show how to install system dependencies, create a venv, export GTK runtime paths, and run the app.

#### 1) Install Homebrew

If you do not have Homebrew installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After the installation, ensure your shell sees Homebrew:

```bash
eval "$($(uname -m | grep -q arm64 && echo /opt/homebrew/bin/brew || echo /usr/local/bin/brew) shellenv)"
```

#### 2) Install system GTK stack and tools

```bash
brew update
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c sshpass
```

**Note:** `sshpass` is now available directly from Homebrew, no custom tap needed.

#### 3) Clone the repository

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
```

#### 4) Create a Python virtual environment (with system GI visible)

Using system-site-packages lets Python see the GI modules installed by Homebrew.

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
```

#### 5) Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

#### 6) Ensure Homebrew tools are on PATH

Put Homebrew bin first on PATH. This works on both Apple Silicon (/opt/homebrew) and Intel (/usr/local):

```bash
export PATH="$(brew --prefix)/bin:$PATH"
```

#### 7) Activate venv and export GTK runtime environment (required on macOS)

Activate your venv and export GI/GTK paths so Python can locate the Homebrew libraries and typelibs:

```bash
source .venv/bin/activate

BREW_PREFIX="$(brew --prefix)"
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

# Set up environment variables for PyGObject and GTK4
export PYTHONPATH="$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages:$PYTHONPATH"
export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib:$DYLD_LIBRARY_PATH"
export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"
export XDG_DATA_DIRS="$BREW_PREFIX/share:$XDG_DATA_DIRS"
```

Quick sanity checks:

```bash
which sshpass && sshpass -V
python -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); gi.require_version("Vte","3.91"); from gi.repository import Gtk,Adw,Vte; import paramiko, cryptography, keyring; print("Environment OK")'
```

#### 8) Run the app

```bash
python run.py
```

Passwords you save will be stored in macOS Keychain via the Python keyring backend. On Linux, SecretStorage/libsecret is used instead.

---

### Troubleshooting

#### sshpass "not found" or spawn error
- Ensure it's installed and on PATH:
  ```bash
  which sshpass && sshpass -V
  brew install sshpass
  export PATH="$(brew --prefix)/bin:$PATH"
  ```

#### Namespace 'Vte'/'Adw'/'Gtk' not available
- Verify packages are installed:
  ```bash
  brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme
  ```
- Ensure libadwaita is properly linked:
  ```bash
  brew link --overwrite libadwaita
  ```
- Export the environment variables in step 7, then re-run.

#### ModuleNotFoundError: No module named 'gi'
- This usually means the environment variables aren't set correctly
- Run the setup script: `./setup_dev_env.sh`
- Or manually set the environment variables as shown in step 7

#### GSettings schema not found (info logs)
- These are informational; the app will use a JSON config file instead.

#### Apple Silicon vs Intel paths
- Apple Silicon Homebrew prefix: `/opt/homebrew`
- Intel Homebrew prefix: `/usr/local`
- `brew --prefix` returns the correct one; prefer using that in PATH/vars.

#### Permission denied errors
- Make sure scripts are executable:
  ```bash
  chmod +x sshpilot-mac.sh
  chmod +x setup_dev_env.sh
  ```

### Development Setup

For development, you can use the development setup script:

```bash
./setup_dev_env.sh
```

This script will:
- Install all required dependencies
- Set up environment variables
- Test the setup
- Provide instructions for permanent environment setup

### Notes

- Passwords you save will be stored in macOS Keychain via the Python keyring backend. On Linux, SecretStorage/libsecret is used instead.
- The launcher script (`sshpilot-mac.sh`) is the recommended way to run the application as it handles all setup automatically.
- For development, use `setup_dev_env.sh` to set up the environment once, then you can run `python3 run.py` directly.



