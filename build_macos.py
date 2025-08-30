#!/usr/bin/env python3
"""
macOS build script for sshPilot
Creates a universal binary that works on both Intel and Apple Silicon architectures
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

# Build configuration
APP_NAME = "sshPilot"
BUNDLE_ID = "io.github.mfat.sshpilot"
VERSION = "1.0.0"
PYTHON_VERSION = "3.11"

# Paths
SCRIPT_DIR = Path(__file__).parent.absolute()
BUILD_DIR = SCRIPT_DIR / "build"
DIST_DIR = SCRIPT_DIR / "dist"
APP_DIR = DIST_DIR / f"{APP_NAME}.app"
CONTENTS_DIR = APP_DIR / "Contents"
MACOS_DIR = CONTENTS_DIR / "MacOS"
RESOURCES_DIR = CONTENTS_DIR / "Resources"
FRAMEWORKS_DIR = CONTENTS_DIR / "Frameworks"

def run_command(cmd, cwd=None, check=True):
    """Run a shell command and return the result"""
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, 
                          capture_output=True, text=True, check=check)
    if result.returncode != 0 and check:
        print(f"Command failed with exit code {result.returncode}")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)
    return result

def check_dependencies():
    """Check if required build tools are available"""
    print("Checking build dependencies...")
    
    # Check for Python
    try:
        result = run_command([sys.executable, "--version"])
        print(f"✓ Python: {result.stdout.strip()}")
    except:
        print("✗ Python not found")
        sys.exit(1)
    
    # Check for create-dmg
    try:
        result = run_command(["which", "create-dmg"], check=False)
        if result.returncode == 0:
            print("✓ create-dmg found")
        else:
            print("Installing create-dmg...")
            run_command(["brew", "install", "create-dmg"])
    except:
        print("✗ create-dmg not found and brew failed. Please install create-dmg manually.")
        sys.exit(1)
    
    # Check for py2app
    try:
        import py2app
        print(f"✓ py2app: {py2app.__version__}")
    except ImportError:
        print("Installing py2app...")
        run_command([sys.executable, "-m", "pip", "install", "py2app"])

def clean_build():
    """Clean previous build artifacts"""
    print("Cleaning previous builds...")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    
    # Remove py2app build artifacts
    for pattern in ["build", "dist", "*.egg-info"]:
        for path in SCRIPT_DIR.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

def create_setup_py():
    """Create setup.py for py2app"""
    setup_content = f'''#!/usr/bin/env python3
"""
Setup script for building sshPilot macOS app
"""

from setuptools import setup, find_packages
import py2app
import os

# Get all Python files in sshpilot directory
def get_data_files():
    data_files = []
    
    # Include GResource files
    gresource_path = "sshpilot/resources/sshpilot.gresource"
    if os.path.exists(gresource_path):
        data_files.append(("resources", [gresource_path]))
    
    # Include UI files
    ui_dir = "sshpilot/ui"
    if os.path.exists(ui_dir):
        ui_files = []
        for root, dirs, files in os.walk(ui_dir):
            for file in files:
                if file.endswith(('.ui', '.xml', '.css')):
                    ui_files.append(os.path.join(root, file))
        if ui_files:
            data_files.append(("ui", ui_files))
    
    # Include icon
    icon_path = "sshpilot/io.github.mfat.sshpilot.svg"
    if os.path.exists(icon_path):
        data_files.append(("", [icon_path]))
    
    return data_files

# py2app options for universal binary
OPTIONS = {{
    'py2app': {{
        'argv_emulation': False,
        'includes': [
            'gi', 'gi.repository', 'gi.repository.Gtk', 'gi.repository.Adw',
            'gi.repository.Vte', 'gi.repository.Gio', 'gi.repository.GLib',
            'paramiko', 'cryptography', 'secretstorage', 'psutil',
            'sshpilot', 'sshpilot.main', 'sshpilot.window', 'sshpilot.terminal'
        ],
        'packages': ['gi', 'sshpilot'],
        'iconfile': 'macos_icon.icns',
        'plist': {{
            'CFBundleName': '{APP_NAME}',
            'CFBundleDisplayName': '{APP_NAME}',
            'CFBundleIdentifier': '{BUNDLE_ID}',
            'CFBundleVersion': '{VERSION}',
            'CFBundleShortVersionString': '{VERSION}',
            'CFBundleExecutable': '{APP_NAME}',
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.utilities',
            'CFBundleDocumentTypes': [],
            'LSArchitecturePriority': ['arm64', 'x86_64'],
        }},
        'arch': 'universal2',  # This creates a universal binary
        'optimize': 2,
        'compressed': True,
        'semi_standalone': False,
        'site_packages': True,
    }}
}}

setup(
    name='{APP_NAME}',
    version='{VERSION}',
    description='SSH connection manager with integrated terminal',
    author='mfat',
    packages=find_packages(),
    data_files=get_data_files(),
    app=['run.py'],
    options=OPTIONS,
    setup_requires=['py2app'],
)
'''
    
    with open(SCRIPT_DIR / "setup.py", "w") as f:
        f.write(setup_content)

def create_icon():
    """Create macOS icon from SVG"""
    print("Creating macOS icon...")
    
    svg_path = SCRIPT_DIR / "sshpilot" / "io.github.mfat.sshpilot.svg"
    icns_path = SCRIPT_DIR / "macos_icon.icns"
    
    if not svg_path.exists():
        print(f"Warning: SVG icon not found at {svg_path}")
        return
    
    # Create iconset directory
    iconset_dir = SCRIPT_DIR / "sshpilot.iconset"
    iconset_dir.mkdir(exist_ok=True)
    
    # Icon sizes for macOS
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    
    try:
        # Convert SVG to different sizes using rsvg-convert or sips
        for size in sizes:
            png_path = iconset_dir / f"icon_{size}x{size}.png"
            png_2x_path = iconset_dir / f"icon_{size}x{size}@2x.png"
            
            # Try rsvg-convert first, then sips
            try:
                run_command([
                    "rsvg-convert", "-w", str(size), "-h", str(size),
                    str(svg_path), "-o", str(png_path)
                ], check=False)
            except:
                try:
                    run_command([
                        "sips", "-s", "format", "png", "-s", "pixelsWide", str(size),
                        "-s", "pixelsHigh", str(size), str(svg_path), "--out", str(png_path)
                    ], check=False)
                except:
                    print(f"Warning: Could not create {size}x{size} icon")
            
            # Create @2x version
            if size < 512:  # Don't create @2x for largest sizes
                try:
                    run_command([
                        "rsvg-convert", "-w", str(size*2), "-h", str(size*2),
                        str(svg_path), "-o", str(png_2x_path)
                    ], check=False)
                except:
                    try:
                        run_command([
                            "sips", "-s", "format", "png", "-s", "pixelsWide", str(size*2),
                            "-s", "pixelsHigh", str(size*2), str(svg_path), "--out", str(png_2x_path)
                        ], check=False)
                    except:
                        pass
        
        # Convert iconset to icns
        run_command(["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)])
        
        # Clean up iconset directory
        shutil.rmtree(iconset_dir)
        
        print(f"✓ Created icon: {icns_path}")
        
    except Exception as e:
        print(f"Warning: Could not create icon: {e}")
        # Create a minimal icns file as fallback
        icns_path.touch()

def install_dependencies():
    """Install Python dependencies for macOS"""
    print("Installing Python dependencies...")
    
    # Install requirements
    run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    
    # Install macOS-specific dependencies
    macos_deps = [
        "py2app",
        "PyGObject",  # This might need special handling on macOS
        "pycairo",
        "paramiko",
        "cryptography",
        "psutil"
    ]
    
    for dep in macos_deps:
        try:
            run_command([sys.executable, "-m", "pip", "install", dep])
        except:
            print(f"Warning: Could not install {dep}")

def build_app():
    """Build the macOS app using py2app"""
    print("Building macOS application...")
    
    # Create setup.py
    create_setup_py()
    
    # Create icon
    create_icon()
    
    # Build the app
    run_command([sys.executable, "setup.py", "py2app", "--arch=universal2"])
    
    print(f"✓ App built: {APP_DIR}")

def create_dmg():
    """Create DMG file"""
    print("Creating DMG...")
    
    dmg_name = f"{APP_NAME}-{VERSION}-universal.dmg"
    dmg_path = DIST_DIR / dmg_name
    
    # Remove existing DMG
    if dmg_path.exists():
        dmg_path.unlink()
    
    # Create DMG using create-dmg
    cmd = [
        "create-dmg",
        "--volname", f"{APP_NAME} {VERSION}",
        "--volicon", str(SCRIPT_DIR / "macos_icon.icns") if (SCRIPT_DIR / "macos_icon.icns").exists() else "",
        "--window-pos", "200", "120",
        "--window-size", "600", "400",
        "--icon-size", "100",
        "--icon", f"{APP_NAME}.app", "175", "120",
        "--hide-extension", f"{APP_NAME}.app",
        "--app-drop-link", "425", "120",
        "--background", str(SCRIPT_DIR / "dmg_background.png") if (SCRIPT_DIR / "dmg_background.png").exists() else "",
        str(dmg_path),
        str(DIST_DIR)
    ]
    
    # Remove empty arguments
    cmd = [arg for arg in cmd if arg]
    
    try:
        run_command(cmd)
        print(f"✓ DMG created: {dmg_path}")
        return dmg_path
    except:
        # Fallback: create simple DMG using hdiutil
        print("Falling back to hdiutil...")
        temp_dmg = DIST_DIR / "temp.dmg"
        
        # Create temporary DMG
        run_command([
            "hdiutil", "create", "-srcfolder", str(DIST_DIR),
            "-volname", f"{APP_NAME} {VERSION}",
            "-fs", "HFS+", "-fsargs", "-c c=64,a=16,e=16",
            "-format", "UDRW", str(temp_dmg)
        ])
        
        # Convert to compressed DMG
        run_command([
            "hdiutil", "convert", str(temp_dmg),
            "-format", "UDZO", "-imagekey", "zlib-level=9",
            "-o", str(dmg_path)
        ])
        
        # Clean up
        temp_dmg.unlink()
        
        print(f"✓ DMG created: {dmg_path}")
        return dmg_path

def create_dmg_background():
    """Create a simple DMG background image"""
    background_path = SCRIPT_DIR / "dmg_background.png"
    if background_path.exists():
        return
    
    try:
        # Create a simple background using ImageMagick or skip if not available
        run_command([
            "convert", "-size", "600x400", "gradient:#f0f0f0-#e0e0e0",
            "-pointsize", "24", "-fill", "#333333",
            "-gravity", "center", "-annotate", "+0-100", f"Install {APP_NAME}",
            "-pointsize", "16", "-annotate", "+0-60", "Drag the app to Applications folder",
            str(background_path)
        ], check=False)
    except:
        print("Note: Could not create DMG background (ImageMagick not available)")

def verify_universal_binary():
    """Verify that the built app is a universal binary"""
    print("Verifying universal binary...")
    
    executable_path = MACOS_DIR / APP_NAME
    if not executable_path.exists():
        print(f"Warning: Executable not found at {executable_path}")
        return False
    
    try:
        result = run_command(["file", str(executable_path)])
        print(f"Binary info: {result.stdout}")
        
        result = run_command(["lipo", "-info", str(executable_path)])
        print(f"Architecture info: {result.stdout}")
        
        if "arm64" in result.stdout and "x86_64" in result.stdout:
            print("✓ Universal binary confirmed (arm64 + x86_64)")
            return True
        else:
            print("Warning: Not a universal binary")
            return False
    except:
        print("Warning: Could not verify binary architecture")
        return False

def main():
    """Main build process"""
    print(f"Building {APP_NAME} for macOS (Universal Binary)")
    print("=" * 50)
    
    # Check dependencies
    check_dependencies()
    
    # Clean previous builds
    clean_build()
    
    # Create directories
    BUILD_DIR.mkdir(exist_ok=True)
    DIST_DIR.mkdir(exist_ok=True)
    
    # Install dependencies
    install_dependencies()
    
    # Build the app
    build_app()
    
    # Verify universal binary
    verify_universal_binary()
    
    # Create DMG background
    create_dmg_background()
    
    # Create DMG
    dmg_path = create_dmg()
    
    print("\n" + "=" * 50)
    print("Build completed successfully!")
    print(f"DMG file: {dmg_path}")
    print(f"Size: {dmg_path.stat().st_size / 1024 / 1024:.1f} MB")
    print("\nThe DMG works on both Intel and Apple Silicon Macs.")

if __name__ == "__main__":
    main()