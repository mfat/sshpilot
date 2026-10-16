# Nuitka build (experimental)

sshPilot ships the PyInstaller build (`..\README.md`). This folder builds the
same app folder with [Nuitka](https://nuitka.net/) for comparison. The
`Windows (Nuitka, experimental)` workflow runs it on manual dispatch only.

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\nuitka\build.ps1
```

The PyInstaller build has to run first: the Nuitka build takes from it the
`share\` data (schemas, icon themes, locale) that PyInstaller's hooks select,
so both bundles carry the same GTK data. It needs `pip install Nuitka==4.3` in
`.venv-win` and MSVC with a Windows SDK (Visual Studio Installer → Desktop
development with C++ → a Windows 11 SDK). The result is
`build\nuitka\sshPilot.dist\`.

## Measured (October 2026, Windows 10, Python 3.14)

Median time from launch to the visible main window, the two builds launched
alternately:

| | PyInstaller | Nuitka |
| --- | --- | --- |
| GUI startup | 2.20 s | 1.79 s |
| `sshpilot-cli.exe --daemon --help` | 0.71 s | 0.44 s |
| App folder | 179 MB | 321 MB |
| Build | 2.5 min | 40 min (3 h before `sshpilot.log_viewer` was kept as bytecode) |

The MSVC object cache (clcache, kept by the workflow) got no hits when
rebuilding unchanged source locally, so expect every build to take the full
time.

## What differs from PyInstaller

| | Why | Where |
| --- | --- | --- |
| `sys.frozen`, `XDG_DATA_DIRS` | Nuitka never sets `sys.frozen` (it marks compiled code with `__compiled__`) and has no runtime hooks. Without `XDG_DATA_DIRS` GTK searches GLib's default data directories and the first window comes up about 0.5 s later. | `sshPilot.py` |
| gi namespaces without a typelib | Nuitka 4.3's gi plugin aborts on Vte, Secret, WebKit and friends instead of skipping them. | `sshpilot_nuitka_plugin.py` |
| `sshpilot.log_viewer` as bytecode | MSVC took 2 h 23 min on its generated C. | `sshpilot_nuitka_plugin.py` |
| GTK libraries read from the typelibs | gi loads each namespace's library from the typelib at runtime, so Nuitka's DLL scan of `_gi.pyd` does not see them, and its gi plugin only names GTK 3 libraries. | `sshpilot_nuitka_plugin.py` |
| libvterm, ConPTY, OpenConsole, gspawn helpers, SVG pixbuf loader declared as DLLs/EXEs | Loaded by path, not linked. Nuitka's manual: DLLs and EXEs are not data files (`--include-data-*` leaves them out). | `sshpilot_nuitka_plugin.py` |
| `sshpilot-cli.exe` is a copy of `sshPilot.exe` with the console subsystem | One compile makes one executable, and neither multidist (`--main` twice) nor `--windows-console-mode=attach` gives a GUI and a console binary. It is 87 MB; a small launcher would save that. | `build.ps1` |

Troubleshooting:

- "Windows SDK must be installed" although it is: delete
  `%LOCALAPPDATA%\Nuitka\Nuitka\Cache\scons-msvc-config`. Nuitka caches the
  MSVC environment and keeps using one taken before the SDK existed.
- Nuitka's tips recommend excluding the build output from Windows Defender
  and the indexing service; their file locks can break resource updates.
