@echo off
REM Build script for SSHPilot Windows PyInstaller bundle
REM This script must be run from within an MSYS2 MinGW64 environment

echo.
echo ============================================
echo Building SSHPilot for Windows
echo ============================================
echo.

REM Check if we're in the project root
if not exist "packaging\pyinstaller\sshpilot-windows.spec" (
    echo ERROR: packaging\pyinstaller\sshpilot-windows.spec not found
    echo Please run this script from the project root directory
    exit /b 1
)

REM Check if virtual environment exists
if not exist ".venv-windows" (
    echo Creating Python virtual environment...
    python -m venv .venv-windows
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        echo Please ensure Python 3.12+ is installed
        exit /b 1
    )
    echo Virtual environment created successfully
)

REM Activate virtual environment
echo Activating virtual environment...
call .venv-windows\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    exit /b 1
)

REM Install/upgrade pip and build tools
echo.
echo Installing/upgrading pip and build tools...
python -m pip install --upgrade pip wheel
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip
    exit /b 1
)

REM Install PyInstaller and dependencies
echo.
echo Installing PyInstaller and hooks...
pip install PyInstaller pyinstaller-hooks-contrib
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller
    exit /b 1
)

REM Install runtime dependencies
echo.
echo Installing runtime dependencies...
pip install cryptography keyring psutil certifi pykeepass PyYAML
if errorlevel 1 (
    echo ERROR: Failed to install runtime dependencies
    exit /b 1
)

REM Install PyGObject and Cairo (these should come from MSYS2)
echo.
echo Checking PyGObject installation...
python -c "import gi; print(f'PyGObject {gi.__version__} found')" 2>nul
if errorlevel 1 (
    echo ERROR: PyGObject not found
    echo Please install it via MSYS2: pacman -S mingw-w64-x86_64-python-gobject
    exit /b 1
)

REM Check for GTK4
echo Checking GTK4 installation...
python -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print(f'GTK4 {Gtk.get_major_version()}.{Gtk.get_minor_version()} found')" 2>nul
if errorlevel 1 (
    echo ERROR: GTK4 not found
    echo Please install it via MSYS2: pacman -S mingw-w64-x86_64-gtk4 mingw-w64-x86_64-libadwaita
    exit /b 1
)

REM Set MSYS2 prefix if not already set
if "%MSYS2_PREFIX%"=="" (
    set MSYS2_PREFIX=C:\msys64\mingw64
    echo Using default MSYS2_PREFIX: %MSYS2_PREFIX%
)

REM Run PyInstaller
echo.
echo ============================================
echo Running PyInstaller...
echo ============================================
echo.
python -m PyInstaller --clean --noconfirm packaging\pyinstaller\sshpilot-windows.spec
if errorlevel 1 (
    echo.
    echo ============================================
    echo ERROR: Build failed
    echo ============================================
    exit /b 1
)

REM Check if build was successful
if not exist "dist\SSHPilot\SSHPilot.exe" (
    echo.
    echo ============================================
    echo ERROR: Build failed - SSHPilot.exe not found
    echo ============================================
    exit /b 1
)

echo.
echo ============================================
echo Build successful!
echo ============================================
echo.
echo Executable: dist\SSHPilot\SSHPilot.exe
echo.
echo You can now:
echo   1. Test the app: cd dist\SSHPilot ^&^& SSHPilot.exe
echo   2. Create installer with NSIS or similar
echo   3. Create portable ZIP: 7z a SSHPilot-Windows.zip dist\SSHPilot\
echo.

REM Create a simple ZIP if 7z is available
where 7z >nul 2>nul
if %errorlevel%==0 (
    echo Creating portable ZIP...
    cd dist
    7z a -tzip SSHPilot-Windows-portable.zip SSHPilot\
    cd ..
    if exist "dist\SSHPilot-Windows-portable.zip" (
        echo Portable ZIP created: dist\SSHPilot-Windows-portable.zip
    )
) else (
    echo 7z not found, skipping ZIP creation
    echo Install 7-Zip to automatically create portable ZIP
)

echo.
echo Done!
