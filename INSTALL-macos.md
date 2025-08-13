## macOS Installation and Run Guide

This guide shows two ways to run sshPilot on macOS (Apple Silicon or Intel):

### Quick start (one command)

Run the helper script to install dependencies, set up a venv, and launch the app. It is saved to `~/sshpilot`.

Download and run directly:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mfat/sshpilot/mac/scripts/install-run-macos.sh)
```

Optionally choose a target directory:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mfat/sshpilot/mac/scripts/install-run-macos.sh) "$HOME/sshpilot"
```

Next time, launch with:

```bash
bash "$HOME/sshpilot/scripts/run-macos.sh"
```

— or —

### Manual installation (venv-based)

The steps below show how to install system dependencies, create a venv, export GTK runtime paths, and run the app.

### 1) Install Homebrew

If you do not have Homebrew installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After the installation, ensure your shell sees Homebrew:

```bash
eval "$($(uname -m | grep -q arm64 && echo /opt/homebrew/bin/brew || echo /usr/local/bin/brew) shellenv)"
```

### 1) Install system GTK stack and tools

```bash
brew update
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config glib graphene icu4c
```

### 2) Install sshpass (required for saved password auto-fill)

```bash
brew install hudochenkov/sshpass/sshpass
```

### 3) Clone the repository (mac branch)

```bash
git clone --branch mac --single-branch https://github.com/mfat/sshpilot.git
cd sshpilot
# If you already have a clone, switch to the mac branch:
# git fetch origin && git checkout mac && git pull --rebase
```

### 4) Create a Python virtual environment (with system GI visible)

Using system-site-packages lets Python see the GI modules installed by Homebrew.

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
```

### 5) Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 6) Ensure Homebrew tools are on PATH

Put Homebrew bin first on PATH. This works on both Apple Silicon (/opt/homebrew) and Intel (/usr/local):

```bash
export PATH="$(brew --prefix)/bin:$PATH"
```

### 7) Activate venv and export GTK runtime environment (required on macOS)

Activate your venv and export GI/GTK paths so Python can locate the Homebrew libraries and typelibs:

```bash
source .venv/bin/activate

BREW_PREFIX="$(brew --prefix)"
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/opt/gtk4/lib:$BREW_PREFIX/opt/glib/lib:$BREW_PREFIX/opt/vte3/lib:$BREW_PREFIX/opt/icu4c/lib:$BREW_PREFIX/opt/graphene/lib:$BREW_PREFIX/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export XDG_DATA_DIRS="$BREW_PREFIX/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
```

Quick sanity checks:

```bash
which sshpass && sshpass -V
python -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); gi.require_version("Vte","3.91"); from gi.repository import Gtk,Adw,Vte; import paramiko, cryptography, keyring; print("Environment OK")'
```

### 8) Run the app

```bash
python run.py
```

Passwords you save will be stored in macOS Keychain via the Python keyring backend. On Linux, SecretStorage/libsecret is used instead.

---

### Troubleshooting

- sshpass “not found” or spawn error
  - Ensure it’s installed and on PATH:
    ```bash
    which sshpass && sshpass -V
    brew reinstall hudochenkov/sshpass/sshpass
    export PATH="$(brew --prefix)/bin:$PATH"
    ```

- Namespace ‘Vte’/‘Adw’/‘Gtk’ not available
  - Verify packages are installed:
    ```bash
    brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection
    ```
  - Export the env vars in step 8, then re-run.

- GSettings schema not found (info logs)
  - These are informational; the app will use a JSON config file instead.

- Apple Silicon vs Intel paths
  - Apple Silicon Homebrew prefix: `/opt/homebrew`
  - Intel Homebrew prefix: `/usr/local`
  - `brew --prefix` returns the correct one; prefer using that in PATH/vars.

### Notes

- Passwords you save will be stored in macOS Keychain via the Python keyring backend. On Linux, SecretStorage/libsecret is used instead.



