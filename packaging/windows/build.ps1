# Build the Windows app folder (and, with -Installer, the NSIS installer).
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-Installer]
#
# Needs a checkout set up by scripts\setup-windows.ps1 (vendor\gtk,
# vendor\conpty, vendor\libvterm) and PyInstaller in .venv-win. The result is
# build\windows\dist\sshPilot\ with sshPilot.exe and sshpilot-cli.exe.

param([switch]$Installer)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$python = Join-Path $root ".venv-win\Scripts\python.exe"
$env:PATH = "$root\vendor\gtk\bin;$env:PATH"

& $python -m PyInstaller --noconfirm --clean `
    --distpath build\windows\dist --workpath build\windows\work `
    packaging\windows\sshpilot-windows.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

& $python packaging\windows\verify_bundle.py build\windows\dist\sshPilot
if ($LASTEXITCODE -ne 0) { throw "bundle verification failed" }

if ($Installer) {
    $makensis = (Get-Command makensis.exe -ErrorAction SilentlyContinue).Source
    if (-not $makensis) { $makensis = "${env:ProgramFiles(x86)}\NSIS\makensis.exe" }
    if (-not (Test-Path $makensis)) { throw "NSIS (makensis.exe) is not installed" }
    $version = & $python -c "import sys; sys.path.insert(0, 'src'); from sshpilot import __version__; print(__version__)"
    # The bundle is built for the Python running PyInstaller; name it like the
    # Linux assets (x86_64, arm64).
    $machine = & $python -c "import platform; print(platform.machine())"
    $arch = @{ "AMD64" = "x86_64"; "ARM64" = "arm64"; "x86" = "x86" }[$machine]
    if (-not $arch) { throw "unknown Windows architecture: $machine" }
    # Absolute script path: makensis changes into the script's directory, so a
    # relative one would make ${__FILEDIR__} resolve twice.
    & $makensis "/DVERSION=$version" "/DSOURCE=$root\build\windows\dist\sshPilot" "/DOUTFILE=$root\build\windows\SSHPilot-$version-windows-$arch-setup.exe" "$PSScriptRoot\installer.nsi"
    if ($LASTEXITCODE -ne 0) { throw "makensis failed" }
}
