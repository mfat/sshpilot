#!/usr/bin/env python3
"""
Universal binary builder for sshPilot on macOS
This script creates a proper universal binary that works on both Intel and Apple Silicon
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

def run_cmd(cmd, check=True, cwd=None):
    """Run command and return result"""
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, shell=isinstance(cmd, str), check=check, 
                          capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0 and check:
        print(f"Error: {result.stderr}")
        sys.exit(1)
    return result

def setup_py2app_config():
    """Create optimized setup.py for universal binary"""
    setup_py_content = '''
from setuptools import setup, find_packages
import py2app
import os
import sys

# Ensure we can import our package
sys.path.insert(0, os.path.dirname(__file__))

APP = ['run.py']
DATA_FILES = []

# Include all sshpilot package files
def collect_package_data():
    data_files = []
    
    # Include GResource files
    for root, dirs, files in os.walk('sshpilot'):
        for file in files:
            if file.endswith(('.gresource', '.ui', '.xml', '.css', '.svg')):
                rel_path = os.path.relpath(os.path.join(root, file), 'sshpilot')
                data_files.append(os.path.join(root, file))
    
    return data_files

# Collect all data files
for data_file in collect_package_data():
    DATA_FILES.append(data_file)

OPTIONS = {
    'py2app': {
        'arch': 'universal2',  # Creates universal binary for both architectures
        'argv_emulation': False,
        'semi_standalone': True,
        'site_packages': True,
        'optimize': 2,
        'compressed': True,
        'iconfile': 'app_icon.icns',
        'plist': {
            'CFBundleName': 'sshPilot',
            'CFBundleDisplayName': 'sshPilot',
            'CFBundleIdentifier': 'io.github.mfat.sshpilot',
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleExecutable': 'sshPilot',
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.utilities',
            'NSRequiresAquaSystemAppearance': False,
            'LSArchitecturePriority': ['arm64', 'x86_64'],
            'NSSupportsAutomaticGraphicsSwitching': True,
        },
        'includes': [
            'gi', 'gi.repository', 'gi.repository.Gtk', 'gi.repository.Adw',
            'gi.repository.Vte', 'gi.repository.Gio', 'gi.repository.GLib',
            'gi.repository.Pango', 'gi.repository.GdkPixbuf',
            'paramiko', 'cryptography', 'psutil', 'threading', 'queue',
            'sshpilot', 'sshpilot.main'
        ],
        'packages': ['gi', 'sshpilot', 'paramiko', 'cryptography'],
        'excludes': ['tkinter', 'test', 'unittest', 'distutils'],
        'resources': DATA_FILES,
    }
}

setup(
    name='sshPilot',
    app=APP,
    data_files=DATA_FILES,
    options=OPTIONS,
    setup_requires=['py2app'],
    install_requires=[
        'PyGObject>=3.42',
        'pycairo>=1.20.0',
        'paramiko>=3.4',
        'cryptography>=42.0',
        'psutil>=5.9.0',
    ],
)
'''
    
    with open('setup.py', 'w') as f:
        f.write(setup_py_content)

def create_app_icon():
    """Create app icon from SVG"""
    svg_path = Path('sshpilot/io.github.mfat.sshpilot.svg')
    icns_path = Path('app_icon.icns')
    
    if not svg_path.exists():
        print(f"Warning: SVG icon not found at {svg_path}")
        # Create a minimal icon
        icns_path.touch()
        return
    
    print("Creating app icon...")
    
    # Create iconset
    iconset_dir = Path('sshPilot.iconset')
    iconset_dir.mkdir(exist_ok=True)
    
    # Standard macOS icon sizes
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    
    for size in sizes:
        png_file = iconset_dir / f'icon_{size}x{size}.png'
        png_2x_file = iconset_dir / f'icon_{size}x{size}@2x.png'
        
        # Convert using sips (built into macOS)
        try:
            run_cmd([
                'sips', '-s', 'format', 'png',
                '-s', 'pixelsWide', str(size),
                '-s', 'pixelsHigh', str(size),
                str(svg_path), '--out', str(png_file)
            ], check=False)
            
            # Create @2x version for retina displays
            if size <= 512:
                run_cmd([
                    'sips', '-s', 'format', 'png',
                    '-s', 'pixelsWide', str(size * 2),
                    '-s', 'pixelsHigh', str(size * 2),
                    str(svg_path), '--out', str(png_2x_file)
                ], check=False)
        except:
            print(f"Warning: Could not create {size}x{size} icon")
    
    # Convert to icns
    try:
        run_cmd(['iconutil', '-c', 'icns', str(iconset_dir)])
        shutil.move('sshPilot.icns', str(icns_path))
        print(f"✓ Created icon: {icns_path}")
    except:
        print("Warning: Could not create icns file")
        icns_path.touch()
    finally:
        # Clean up
        if iconset_dir.exists():
            shutil.rmtree(iconset_dir)

def main():
    """Build universal binary"""
    print("Setting up universal binary build for sshPilot")
    
    # Clean previous builds
    for path in ['build', 'dist', 'setup.py', '*.icns', '*.iconset']:
        for item in Path('.').glob(path):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    
    # Create icon
    create_app_icon()
    
    # Create setup.py
    setup_py2app_config()
    
    # Install dependencies
    print("Installing build dependencies...")
    run_cmd([sys.executable, '-m', 'pip', 'install', '-r', 'macos_requirements.txt'])
    
    # Build the app
    print("Building universal binary...")
    run_cmd([sys.executable, 'setup.py', 'py2app', '--arch=universal2'])
    
    # Verify the build
    app_path = Path('dist/sshPilot.app')
    if app_path.exists():
        executable = app_path / 'Contents/MacOS/sshPilot'
        if executable.exists():
            print("Verifying universal binary...")
            result = run_cmd(['lipo', '-info', str(executable)], check=False)
            print(f"Architecture info: {result.stdout}")
            
            if 'arm64' in result.stdout and 'x86_64' in result.stdout:
                print("✓ Universal binary created successfully")
            else:
                print("Warning: Binary may not be universal")
        else:
            print(f"Warning: Executable not found at {executable}")
    else:
        print("Error: App bundle not created")
        sys.exit(1)

if __name__ == '__main__':
    main()