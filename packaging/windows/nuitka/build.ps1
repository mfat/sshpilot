# Experimental Nuitka build of the Windows app folder, for comparison with
# the PyInstaller build (the one that ships). See README.md here.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\windows\nuitka\build.ps1
#
# Run the PyInstaller build first: this takes from it the share\ data
# (schemas, icon themes, locale) that PyInstaller's hooks select, so both
# bundles carry the same GTK data. The GTK libraries and the vendored
# binaries come in through sshpilot_nuitka_plugin.py. Needs Nuitka in
# .venv-win and MSVC with a Windows SDK. The result is
# build\nuitka\sshPilot.dist\ with sshPilot.exe and sshpilot-cli.exe.

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $root
$python = Join-Path $root ".venv-win\Scripts\python.exe"
$internal = "build\windows\dist\sshPilot\_internal"
$out = "build\nuitka"
$dist = "$out\sshPilot.dist"
$gtk = "vendor\gtk"

if (-not (Test-Path $internal)) { throw "missing ${internal}: run packaging\windows\build.ps1 first" }
$env:PATH = "$root\$gtk\bin;$env:PATH"
# The entry point imports run.py from the checkout root.
$env:PYTHONPATH = "$root;$root\src"
$version = & $python -c "from sshpilot import __version__; print(__version__)"

if (Test-Path $out) { Remove-Item -Recurse -Force $out }
& $python -m nuitka --mode=standalone --msvc=latest --assume-yes-for-downloads --no-progressbar `
    "--user-plugin=$PSScriptRoot\sshpilot_nuitka_plugin.py" `
    "--user-package-configuration-file=$PSScriptRoot\sshpilot.nuitka-package.config.yml" `
    "--output-dir=$out" --output-filename=sshPilot.exe `
    --windows-console-mode=disable --windows-icon-from-ico=packaging\windows\sshpilot.ico `
    --product-name=sshPilot --file-description=sshPilot "--product-version=$version" "--file-version=$version" `
    --include-module=run `
    --include-package=sshpilot --include-package-data=sshpilot `
    --include-module=keyring.backends.Windows --include-package=win32ctypes `
    --include-module=gi._gi_cairo --include-module=gi.repository.cairo `
    --include-package=pykeepass --include-package=Cryptodome --include-package=argon2 `
    --nofollow-import-to=tkinter "--nofollow-import-to=*.tests" --nofollow-import-to=pytest `
    --nofollow-import-to=keyring.backends.libsecret --nofollow-import-to=keyring.backends.SecretService `
    --nofollow-import-to=keyring.backends.kwallet --nofollow-import-to=keyring.backends.macOS `
    "--include-data-dir=$internal\share=share" `
    "--include-data-dir=$gtk\lib\gdk-pixbuf-2.0=lib\gdk-pixbuf-2.0" `
    "--report=$out\report.xml" `
    "$PSScriptRoot\sshPilot.py"
if ($LASTEXITCODE -ne 0) { throw "Nuitka failed" }

# The console twin for the daemon and askpass (see ..\README.md, "Why two
# executables"): the same binary with its PE subsystem set to console.
& $python -c @"
import shutil, struct, sys
gui, cli = sys.argv[1] + r'\sshPilot.exe', sys.argv[1] + r'\sshpilot-cli.exe'
shutil.copyfile(gui, cli)
with open(cli, 'r+b') as f:
    f.seek(0x3C); pe = struct.unpack('<I', f.read(4))[0]
    f.seek(pe + 24 + 68); f.write(struct.pack('<H', 3))
"@ $dist
if ($LASTEXITCODE -ne 0) { throw "making sshpilot-cli.exe failed" }

$required = @(
    "sshPilot.exe", "sshpilot-cli.exe", "libvterm-0.dll", "conpty.dll", "OpenConsole.exe",
    "gspawn-win64-helper.exe", "gspawn-win64-helper-console.exe",
    "gi\_gi.pyd", "gtk-4-1.dll", "adwaita-1-0.dll", "gtksourceview-5-0.dll", "girepository\Gtk-4.0.typelib",
    "share\glib-2.0\schemas\gschemas.compiled", "lib\gdk-pixbuf-2.0\2.10.0\loaders.cache",
    "lib\gdk-pixbuf-2.0\2.10.0\loaders\pixbufloader_svg.dll",
    "sshpilot\resources\sshpilot.gresource"
)
$missing = $required | Where-Object { -not (Test-Path (Join-Path $dist $_)) }
if ($missing) { throw "bundle is missing: $($missing -join ', ')" }

& "$dist\sshpilot-cli.exe" --daemon --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "sshpilot-cli.exe --daemon --help failed" }
