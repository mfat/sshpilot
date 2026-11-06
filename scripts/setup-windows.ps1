# Set up a Windows development checkout on python.org Python + gvsbuild GTK 4.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
#
# Creates .venv-win, fetches the pinned GTK 4 runtime (vendor\gtk) and
# Microsoft's ConPTY (vendor\conpty), installs the PyGObject/pycairo wheels
# that match the runtime plus the Python dependencies, and builds the patched
# libvterm when a compiler is available. Run the app with run-windows.ps1.
# See docs/running-on-windows.md.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = "py"
$pythonArgs = @("-3.14")
& $python @pythonArgs -c "import sys; assert sys.version_info >= (3, 14)" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.14 from python.org is required (py -3.14). Install it from https://www.python.org/downloads/windows/"
}

if (-not (Test-Path ".venv-win\Scripts\python.exe")) {
    & $python @pythonArgs -m venv .venv-win
}
$venv = Join-Path $root ".venv-win\Scripts\python.exe"

& $venv scripts\fetch-gtk.py
& $venv scripts\fetch-conpty.py

& $venv -m pip install --upgrade pip
$wheels = Get-ChildItem vendor\gtk\wheels -Filter *.whl | Where-Object { $_.Name -match "^(pygobject|pycairo)-.*-cp314-" }
& $venv -m pip install $wheels.FullName
& $venv -m pip install --no-deps -e .
& $venv -m pip install -r requirements.txt pyyaml
& $venv -m pip install -r requirements-dev.txt

# The patched libvterm (the Windows default terminal emulator). Without it the
# built-in terminal falls back to pyte.
if (Get-Command cl.exe -ErrorAction SilentlyContinue) {
    & $venv packaging\windows\libvterm\build.py --compiler msvc
} elseif (Get-Command gcc.exe -ErrorAction SilentlyContinue) {
    & $venv packaging\windows\libvterm\build.py --compiler gcc
} else {
    Write-Warning "No C compiler found: skipping libvterm. Run packaging\windows\libvterm\build.py from a Visual Studio developer prompt to build it."
}

Write-Host "Done. Start the app with: powershell -File scripts\run-windows.ps1"
