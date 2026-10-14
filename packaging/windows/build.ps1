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
    & $makensis "/DVERSION=$version" "/DSOURCE=$root\build\windows\dist\sshPilot" "/DOUTFILE=$root\build\windows\sshPilot-$version-setup.exe" packaging\windows\installer.nsi
    if ($LASTEXITCODE -ne 0) { throw "makensis failed" }
}
