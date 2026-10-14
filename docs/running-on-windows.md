# Running sshPilot from source (Windows)

Windows support is **experimental**. This page covers running from a source
checkout on Windows 10 (1809+) or Windows 11. A packaged build is described in
[packaging/windows](../packaging/windows/README.md).

How the port works, and why: [architecture/windows-port.md](architecture/windows-port.md).

## What is different on Windows

| Area | Linux / macOS | Windows |
| --- | --- | --- |
| GTK runtime | distro / Homebrew | gvsbuild GTK 4 with python.org Python (MSYS2 also works) |
| SSH client | OpenSSH | built-in `C:\Windows\System32\OpenSSH\ssh.exe` |
| Daemon IPC | Unix socket, mode 0600 | loopback TCP plus a per-launch token in `%LOCALAPPDATA%\sshpilot\run\sshpilotd.json` |
| Session terminal | POSIX PTY | ConPTY through `ctypes`, with Microsoft's bundled console |
| Terminal widget | VTE (or xterm.js) | built-in backend drawn with GTK: libvterm (default) or pyte |
| Local shell tabs | the login shell | PowerShell 7, Windows PowerShell, cmd or WSL (Preferences → Terminal → Local Shell) |
| Connection multiplexing | optional ControlMaster | not available (Win32-OpenSSH has no ControlMaster) |
| Secret storage (auto) | libsecret / Keychain | Windows Credential Manager (via `keyring`). KeePass (KDBX) is also available. |
| Serial connections | picocom or screen | PuTTY's `plink` |
| RDP connections | FreeRDP 3 | FreeRDP 3, or Windows' own Remote Desktop client |
| Config directory | `~/.config/sshpilot` | `%APPDATA%\sshpilot` |

Not available on Windows: the xterm.js backend.

## 1. Set up (python.org Python + gvsbuild GTK 4)

Install [Python 3.14 from python.org](https://www.python.org/downloads/windows/)
(64-bit) and Git, then from PowerShell:

```powershell
git clone https://github.com/mfat/sshpilot.git
```

```powershell
cd sshpilot; powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
```

The setup script:

1. creates `.venv-win` with `py -3.14`;
2. downloads the pinned [gvsbuild](https://github.com/wingtk/gvsbuild) GTK 4
   runtime (GTK, libadwaita, GtkSourceView, PyGObject), checks its SHA-256,
   and unpacks it into `vendor\gtk` (`scripts\fetch-gtk.py`);
3. downloads Microsoft's current console host (`conpty.dll` and
   `OpenConsole.exe` from the `Microsoft.Windows.Console.ConPTY` NuGet
   package), checks its hash and signatures, and puts it in `vendor\conpty`
   (`scripts\fetch-conpty.py`). The console built into Windows 10 drops mouse
   input, bracketed paste and the alternate screen;
4. installs the PyGObject and pycairo wheels that match the runtime, then the
   Python dependencies;
5. builds the patched libvterm into `vendor\libvterm` when a C compiler is
   available (`packaging\windows\libvterm\build.py`: `--compiler msvc` from a
   Visual Studio developer prompt, or `--compiler gcc`). Without it the
   terminal uses pyte.

Optional: `.venv-win\Scripts\pip install pykeepass` for KeePass (KDBX) storage.

## 2. Run

```powershell
powershell -File scripts\run-windows.ps1
```

It puts `vendor\gtk\bin` on PATH and runs `run.py`. Add `--verbose` for debug
logging. The app starts its background daemon automatically. To run the daemon
on its own (handy for debugging):

```powershell
powershell -File scripts\run-windows.ps1 --daemon --verbose
```

Overrides, for testing:
- `SSHPILOT_TERMINAL_MODEL=pyte` or `vterm` picks the emulator for one run
  (Preferences → Terminal → Terminal Backend sets it persistently).
- `SSHPILOT_CONPTY_DLL=kernel32` forces the console built into Windows;
  `SSHPILOT_CONPTY_DLL=<directory>` points at another copy.
- `SSHPILOT_CONPTY_BACKEND=pywinpty` brings back the previous pywinpty-based
  runner (install `pywinpty` first).
- `SSHPILOT_LIBVTERM=<path>` loads a specific `libvterm-0.dll`.

Each launch is its own instance: sshPilot does not use a D-Bus session bus on
Windows (a wedged `gdbus.exe` used to hang startup), so a second launch opens
a second window on the same daemon.

## Alternative: MSYS2

The app also runs under MSYS2 UCRT64. Install [MSYS2](https://www.msys2.org/),
open the **MSYS2 UCRT64** shell, run `pacman -Syu`, reopen the shell if asked,
then:

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-libadwaita mingw-w64-ucrt-x86_64-gtksourceview5 mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-cryptography mingw-w64-ucrt-x86_64-python-psutil mingw-w64-ucrt-x86_64-python-yaml mingw-w64-ucrt-x86_64-python-pip git
```

```bash
python -m venv --system-site-packages .venv-msys && .venv-msys/bin/python -m pip install pyte keyring certifi
```

```bash
.venv-msys/bin/python scripts/fetch-conpty.py
```

```bash
.venv-msys/bin/python run.py
```

`scripts/run-windows.sh` does the venv step and runs the app. For libvterm,
either build it (`pacman -S mingw-w64-ucrt-x86_64-gcc`, then
`packaging/windows/libvterm/build.py`) or install the distro DLL
(`mingw-w64-ucrt-x86_64-libvterm`). The distro DLL lacks the CSI bound, so the
model filters CSI parameters in Python for it, at some cost in speed.

## Notes

- **SSH keys:** keys generated by sshPilot get an owner-only ACL. OpenSSH for
  Windows refuses private keys that other users can read, and reports this as
  "UNPROTECTED PRIVATE KEY FILE".
- **Config file permissions:** OpenSSH for Windows also refuses config files
  other principals can reach ("Bad owner or permissions"). The daemon keeps the
  files it writes owner-only.
- **Password prompts:** password and passphrase prompts go through `SSH_ASKPASS`.
  The daemon writes an `askpass.cmd` launcher into a private temp directory for
  each run. Without connection sharing every channel logs in on its own, so the
  password dialog offers "Remember until SSH Pilot quits".
- **ssh-agent:** OpenSSH's agent service is disabled by default. As
  administrator: `Set-Service ssh-agent -StartupType Manual` and
  `Start-Service ssh-agent`. 1Password's agent uses the same pipe.
- **`ControlMaster` in `~/.ssh/config`:** if your config, copied from Linux or
  macOS, sets `ControlMaster`/`ControlPath`, OpenSSH for Windows rejects it.
  Remove those lines or guard them with `Match exec`.
- **Resources:** the compiled `sshpilot.gresource`, `.ui` and translation files
  are committed, so no build step is needed unless you edit them
  (`scripts/build_gresource.sh` needs `blueprint-compiler` and `gettext`).
