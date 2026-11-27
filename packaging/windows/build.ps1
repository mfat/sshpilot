# Build the Windows app folder (and, with -Installer, the NSIS installer;
# with -Portable, a zip of the app folder that runs without installing).
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-Installer] [-Portable]
#
# Needs a checkout set up by scripts\setup-windows.ps1 (vendor\gtk,
# vendor\conpty, vendor\libvterm) and PyInstaller in .venv-win. The result is
# build\windows\dist\sshPilot\ with sshPilot.exe and sshpilot-cli.exe.

param([switch]$Installer, [switch]$Portable)

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

if ($Installer -or $Portable) {
    $version = & $python -c "import sys; sys.path.insert(0, 'src'); from sshpilot import __version__; print(__version__)"
    # The bundle is built for the Python running PyInstaller; name it like the
    # Linux assets (x86_64, arm64).
    $machine = & $python -c "import platform; print(platform.machine())"
    $arch = @{ "AMD64" = "x86_64"; "ARM64" = "arm64"; "x86" = "x86" }[$machine]
    if (-not $arch) { throw "unknown Windows architecture: $machine" }
}

if ($Installer) {
    $makensis = (Get-Command makensis.exe -ErrorAction SilentlyContinue).Source
    if (-not $makensis) { $makensis = "${env:ProgramFiles(x86)}\NSIS\makensis.exe" }
    if (-not (Test-Path $makensis)) { throw "NSIS (makensis.exe) is not installed" }
    # Absolute script path: makensis changes into the script's directory, so a
    # relative one would make ${__FILEDIR__} resolve twice.
    & $makensis "/DVERSION=$version" "/DSOURCE=$root\build\windows\dist\sshPilot" "/DOUTFILE=$root\build\windows\SSHPilot-$version-windows-$arch-setup.exe" "$PSScriptRoot\installer.nsi"
    if ($LASTEXITCODE -ne 0) { throw "makensis failed" }
}

if ($Portable) {
    # The same app folder, zipped: unpack anywhere and run sshPilot.exe. It
    # adds no shortcut and no registry entries; settings and keys still live
    # in the user's profile, as for the installed app.
    $zip = "$root\build\windows\SSHPilot-$version-windows-$arch-portable.zip"
    if (Test-Path $zip) { Remove-Item $zip }
    # tar (bsdtar, in Windows since 10 1803), not Compress-Archive: Windows
    # PowerShell 5.1's stores paths with backslashes, which other unzip
    # tools and the zip format do not accept.
    tar.exe -a -c -f $zip -C "$root\build\windows\dist" sshPilot
    if ($LASTEXITCODE -ne 0) { throw "zipping the portable build failed" }
}
