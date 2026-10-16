# Windows build

A packaged sshPilot for Windows 10 (1809+) and 11, x64: a folder app frozen
with PyInstaller from python.org Python and the gvsbuild GTK 4 runtime, and a
per-user NSIS installer around it.

## Build

From a checkout set up by `scripts\setup-windows.ps1` (it fetches `vendor\gtk`
and `vendor\conpty`; libvterm needs a compiler, see below):

```powershell
.venv-win\Scripts\python.exe -m pip install pyinstaller
```

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Installer
```

The result:

| Path | What |
| --- | --- |
| `build\windows\dist\sshPilot\sshPilot.exe` | the GUI (windowed) |
| `build\windows\dist\sshPilot\sshpilot-cli.exe` | the daemon (`--daemon`), askpass (`--internal-askpass`) and rbw's pinentry (`--internal-rbw-pinentry`); console subsystem |
| `build\windows\dist\sshPilot\_internal\` | Python, GTK 4, typelibs, `libvterm-0.dll`, `conpty.dll`, `OpenConsole.exe` |
| `build\windows\sshPilot-<version>-setup.exe` | the installer (about 60 MiB) |

`build.ps1` runs `verify_bundle.py`, which fails the build when a file the app
loads at runtime (typelibs, schemas, pixbuf loaders, the ctypes-loaded DLLs)
is missing.

## Why two executables

A windowed exe has no console. If the daemon ran from it, every OpenSSH
child it starts with a plain pipe would open a console window, and the
askpass helper OpenSSH runs (`<daemon exe> --internal-askpass`) would have no
stdout to answer on. The launcher starts the daemon from `sshpilot-cli.exe`
when it sits next to the app (`daemon/launcher.py`,
`_frozen_console_executable`).

## libvterm

`packaging\windows\libvterm\build.py --compiler msvc` (in a Visual Studio
developer prompt) builds the patched `libvterm-0.dll` into
`vendor\libvterm\x64`. The Windows workflow does this with
`ilammy/msvc-dev-cmd`. A MinGW build (`--compiler gcc`) also works: it
imports only system UCRT DLLs.

## Installer

`installer.nsi` installs per user into `%LOCALAPPDATA%\Programs\sshPilot` (no
administrator rights), adds a Start-menu shortcut and an entry under
Settings → Apps, and stops a running daemon before replacing files.
Uninstalling removes the program but keeps settings and connections in
`%APPDATA%\sshpilot` and `%LOCALAPPDATA%\sshpilot`. Silent install:
`sshPilot-<version>-setup.exe /S`, optionally `/D=<directory>` (last).

## Testing on a clean Windows

`sandbox-test.ps1` runs the built installer in Windows Sandbox (Windows Pro
or Enterprise; turn on the "Windows Sandbox" optional feature and restart):
a fresh profile, no developer tools on PATH, and no GPU unless `-Gpu` is
given. `sandbox-run.ps1` installs silently, launches the app and writes
`build\windows\sandbox-results\report.txt`, then leaves sshPilot open to try
by hand. The sandbox runs the host's Windows build, so it cannot test older
releases.

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\sandbox-test.ps1
```

## Nuitka (experimental)

`nuitka\` builds the same app folder with Nuitka for comparison; the
`Windows (Nuitka, experimental)` workflow runs it on manual dispatch. See
`nuitka\README.md`.

## Signing and winget (maintainer setup)

Unsigned installers trigger SmartScreen. The release job signs through
[SignPath Foundation](https://signpath.org/) (free for open source) when the
repository has `SIGNPATH_API_TOKEN` and the `SIGNPATH_ORGANIZATION_ID`,
`SIGNPATH_PROJECT_SLUG` and `SIGNPATH_SIGNING_POLICY_SLUG` variables; without
them it publishes the unsigned build as a workflow artifact.

winget: after a signed release, submit the manifest with
`wingetcreate update <id> --urls <installer URL> --version <version> --submit`
(Gaphor's release job does the same).
