## macOS Installation and Run Guide

This guide shows how to install system dependencies, clone the project, set up Python, and run sshPilot on macOS (Apple Silicon or Intel).

### 1) Install Homebrew

If you do not have Homebrew installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After the installation, ensure your shell sees Homebrew:

```bash
eval "$($(uname -m | grep -q arm64 && echo /opt/homebrew/bin/brew || echo /usr/local/bin/brew) shellenv)"
```

### 2) Install system GTK stack and tools

```bash
brew update
brew install gtk4 libadwaita pygobject3 py3cairo vte3 gobject-introspection adwaita-icon-theme pkg-config
```

### 3) Install sshpass (required for saved password auto-fill)

```bash
brew install hudochenkov/sshpass/sshpass
```

### 4) Clone the repository

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
```

### 5) Create a Python virtual environment (with system GI visible)

Using system-site-packages lets Python see the GI modules installed by Homebrew.

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
```

### 6) Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 7) Ensure tools are on PATH (sshpass, GTK tools)

Put Homebrew bin first on PATH. This works on both Apple Silicon (/opt/homebrew) and Intel (/usr/local):

```bash
export PATH="$(brew --prefix)/bin:$PATH"
```

If GObject Introspection can’t find GTK/VTE/Adwaita, also set the typelib path:

```bash
export GI_TYPELIB_PATH="$(brew --prefix)/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
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
  - Export GI_TYPELIB_PATH as shown above, then re-run.

- GSettings schema not found (info logs)
  - These are informational; the app will use a JSON config file instead.

- Apple Silicon vs Intel paths
  - Apple Silicon Homebrew prefix: `/opt/homebrew`
  - Intel Homebrew prefix: `/usr/local`
  - `brew --prefix` returns the correct one; prefer using that in PATH/vars.


