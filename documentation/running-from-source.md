# Running sshPilot from source (Linux)

sshPilot is a GTK4/libadwaita application, so a source checkout needs the GTK
stack and PyGObject in addition to a handful of pure-Python packages. Two
setups are supported on Linux, mirroring the two paths in PyGObject's official
[Getting Started](https://pygobject.gnome.org/getting_started.html) guide:

- **[Approach A — Hybrid](#approach-a--hybrid-system-pygobject--venv-recommended)
  (recommended):** install the GTK stack *and* PyGObject from your distribution,
  then create a venv with `--system-site-packages` and `pip install` only the
  pure-Python deps. No compiler needed; fastest to set up.
- **[Approach B — Pure venv](#approach-b--pure-venv-pip-built-pygobject):**
  install build tools + GTK `-dev` headers, then `pip install pycairo PyGObject`
  (and the rest) into a *plain* venv. Use this if you want the Python stack fully
  isolated from system packages, or need a newer PyGObject than your distro
  ships.

> **Why a venv at all?** Modern Linux distributions ship an *externally-managed*
> system Python (PEP 668) that refuses `pip install`. A venv keeps sshPilot's
> Python dependencies isolated and is the recommended development model.

macOS: see [INSTALL-macos.md](INSTALL-macos.md).

Supported/tested Python versions: **3.12** and **3.13** (CI matrix).

---

## Approach A — Hybrid: system PyGObject + venv (recommended)

### 1. Install system prerequisites

These provide PyGObject, pycairo, the GObject-Introspection (GI) typelibs, and
the native GTK4/libadwaita/VTE/GtkSourceView/WebKit runtime.

**Debian/Ubuntu**

```bash
sudo apt update
sudo apt install \
  python3 python3-venv python3-gi python3-gi-cairo \
  libgtk-4-1 gir1.2-gtk-4.0 \
  libadwaita-1-0 gir1.2-adw-1 \
  libvte-2.91-gtk4-0 gir1.2-vte-3.91 \
  libgtksourceview-5-0 gir1.2-gtksource-5 \
  libsecret-1-0 gir1.2-secret-1 \
  python3-paramiko python3-cryptography sshpass ssh-askpass \
  gir1.2-webkit-6.0
```

**Fedora / RHEL / CentOS**

```bash
sudo dnf install \
  python3 python3-gobject \
  gtk4 libadwaita \
  vte291-gtk4 \
  gtksourceview5 \
  libsecret \
  python3-paramiko python3-cryptography sshpass openssh-askpass \
  webkitgtk6
```

**Arch Linux**

```bash
sudo pacman -S --needed \
  python python-gobject python-cairo \
  gtk4 libadwaita vte4 gtksourceview5 libsecret \
  python-paramiko python-cryptography sshpass
```

On Arch the GObject-Introspection typelibs ship inside the library packages, so
there are no separate `gir`/`typelib` packages to install.

**openSUSE (Tumbleweed)**

```bash
sudo zypper install \
  python3 python3-gobject python3-gobject-Gdk \
  typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-Vte-3_91 \
  typelib-1_0-GtkSource-5 typelib-1_0-Secret-1 \
  python3-paramiko python3-cryptography sshpass openssh-askpass-gnome
```

Installing the `typelib-1_0-*` packages automatically pulls in the matching
runtime libraries (`libgtk-4-1`, `libadwaita-1-0`, …).

> **WebKit is optional.** The GTK4 WebKit 6.0 package (`gir1.2-webkit-6.0` on
> Debian/Ubuntu, `webkitgtk6` on Fedora, `webkitgtk-6.0` on Arch,
> `typelib-1_0-WebKit-6_0` on openSUSE) is only needed for the optional
> **PyXterm.js** terminal backend. The default **VTE** backend runs without it,
> so you can leave it out unless you specifically want that backend.

### 2. Create a venv that can see the system bindings

The `--system-site-packages` flag is **required** so the venv can import the
distribution's `gi`/`cairo` modules:

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
```

(Run `deactivate` to leave the environment later.)

### 3. Install the pure-Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

PyGObject and pycairo are intentionally **not** installed by pip here — they
come from the system packages in step 1.

### 4. Run

```bash
python3 run.py            # add --verbose for debug logging
```

---

## Approach B — Pure venv: pip-built PyGObject

PyGObject and pycairo are compiled from source in this setup, so you need a C
toolchain and the GTK/cairo development headers. The GTK/libadwaita/VTE/
GtkSourceView/WebKit **runtime** libraries and their GI typelibs must still come
from the distribution — pip only builds the PyGObject *bindings*, not GTK itself.

### 1. Install build dependencies + GTK runtime

**Debian/Ubuntu**

```bash
sudo apt update
sudo apt install \
  python3 python3-venv python3-dev gcc pkg-config \
  libgirepository-2.0-dev libcairo2-dev \
  gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 \
  libvte-2.91-gtk4-0 gir1.2-vte-3.91 \
  libgtksourceview-5-0 gir1.2-gtksource-5 \
  libsecret-1-0 gir1.2-secret-1 \
  sshpass ssh-askpass gir1.2-webkit-6.0
```

**Fedora / RHEL / CentOS**

```bash
sudo dnf install \
  python3 python3-devel gcc pkg-config \
  gobject-introspection-devel cairo-gobject-devel \
  gtk4 libadwaita vte291-gtk4 gtksourceview5 libsecret \
  sshpass openssh-askpass webkitgtk6
```

**Arch Linux**

```bash
sudo pacman -S --needed \
  python cairo pkgconf gobject-introspection gcc \
  gtk4 libadwaita vte4 gtksourceview5 libsecret sshpass
```

**openSUSE (Tumbleweed)**

```bash
sudo zypper install \
  python3 python3-devel gcc pkg-config \
  gobject-introspection-devel cairo-devel \
  typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-Vte-3_91 \
  typelib-1_0-GtkSource-5 typelib-1_0-Secret-1 \
  sshpass openssh-askpass-gnome
```

(WebKit 6.0 is optional here too — add `webkitgtk-6.0` on Arch /
`typelib-1_0-WebKit-6_0` on openSUSE only if you want the PyXterm.js backend.)

### 2. Create a plain venv

```bash
git clone https://github.com/mfat/sshpilot.git
cd sshpilot
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Build & install PyGObject + pycairo, then the rest

```bash
pip install --upgrade pip
pip install pycairo PyGObject
pip install -r requirements.txt
```

`requirements.txt` carries only the pure-Python deps; pycairo/PyGObject are
installed explicitly here because this approach builds them from PyPI.

### 4. Run

```bash
python3 run.py            # add --verbose for debug logging
```

---

## Development tasks (either approach)

Inside the activated venv:

```bash
pip install -r requirements-dev.txt   # test + lint tooling
pytest -ra -m "not integration"       # unit suite (as CI runs it)
ruff check sshpilot/ tests/           # lint (as CI runs it)
```

`integration`-marked tests run real tool binaries and are exercised separately
in CI. Some unit tests are marked `xfail` (see `tests/conftest.py`) — that is
expected.

---

## Troubleshooting

- **`ModuleNotFoundError: No module named 'gi'` (Approach A)** — the venv was
  created without `--system-site-packages`. Recreate it with that flag.
- **pip tries to build PyGObject and fails on missing headers (Approach A)** —
  the system bindings aren't installed (or the venv can't see them). Install the
  step-1 packages and ensure the venv has `--system-site-packages`.
- **Compile errors building pycairo/PyGObject (Approach B)** — install the dev
  headers and toolchain (`*-dev`/`*-devel`, `pkg-config`, `gcc`).
- **`externally-managed-environment` error** — you're running pip against the
  system Python. Activate the venv first.

For background on distro package names, see PyGObject's official
[System Dependencies](https://pygobject.gnome.org/guide/sysdeps.html) page.
