@echo off
rem Double-click launcher for a source checkout on Windows.
rem Runs scripts\run-windows.sh inside MSYS2 UCRT64 (see docs\running-on-windows.md).
rem Extra arguments (e.g. --verbose) are passed through to run.py.
setlocal
if "%MSYS2_ROOT%"=="" set "MSYS2_ROOT=C:\msys64"
if not exist "%MSYS2_ROOT%\usr\bin\bash.exe" (
  echo MSYS2 was not found at %MSYS2_ROOT%. Install it or set MSYS2_ROOT.
  pause
  exit /b 1
)
set "MSYSTEM=UCRT64"
set "CHERE_INVOKING=1"
cd /d "%~dp0.."
"%MSYS2_ROOT%\usr\bin\bash.exe" -l scripts/run-windows.sh %*
if errorlevel 1 pause
