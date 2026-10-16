# Run sshPilot from a Windows checkout set up by setup-windows.ps1.
# Extra arguments go to run.py (for example --verbose).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$gtk = Join-Path $root "vendor\gtk\bin"
if (-not (Test-Path $gtk)) {
    throw "vendor\gtk is missing: run scripts\setup-windows.ps1 first."
}
$env:PATH = "$gtk;$env:PATH"
& (Join-Path $root ".venv-win\Scripts\python.exe") (Join-Path $root "run.py") @args
exit $LASTEXITCODE
