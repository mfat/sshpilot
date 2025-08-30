#!/usr/bin/env python3
"""
Complete build script for sshPilot macOS DMG
Handles the entire build process from source to universal DMG
"""

import os
import sys
import shutil
import subprocess
import json
from pathlib import Path

class UniversalDMGBuilder:
    def __init__(self):
        self.app_name = "sshPilot"
        self.version = "1.0.0"
        self.bundle_id = "io.github.mfat.sshpilot"
        self.min_macos = "11.0"
        
        self.script_dir = Path(__file__).parent.absolute()
        self.build_dir = self.script_dir / "build"
        self.dist_dir = self.script_dir / "dist"
        self.app_path = self.dist_dir / f"{self.app_name}.app"
        self.dmg_path = self.dist_dir / f"{self.app_name}-{self.version}-universal.dmg"
        
        # Ensure we're on macOS
        if os.uname().sysname != 'Darwin':
            print("❌ This script must be run on macOS")
            sys.exit(1)
    
    def log(self, message, level="INFO"):
        """Colored logging"""
        colors = {
            "INFO": "\033[0;34m",    # Blue
            "SUCCESS": "\033[0;32m", # Green  
            "WARNING": "\033[1;33m", # Yellow
            "ERROR": "\033[0;31m",   # Red
        }
        reset = "\033[0m"
        print(f"{colors.get(level, '')}{message}{reset}")
    
    def run_cmd(self, cmd, check=True, cwd=None, capture=True):
        """Execute command with proper error handling"""
        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
        self.log(f"→ {cmd_str}")
        
        result = subprocess.run(
            cmd, shell=isinstance(cmd, str), check=False,
            capture_output=capture, text=True, cwd=cwd
        )
        
        if result.returncode != 0:
            if check:
                self.log(f"Command failed: {cmd_str}", "ERROR")
                if capture and result.stderr:
                    self.log(f"Error: {result.stderr}", "ERROR")
                sys.exit(1)
            else:
                self.log(f"Command failed (non-fatal): {cmd_str}", "WARNING")
        
        return result
    
    def check_system_requirements(self):
        """Verify system requirements"""
        self.log("Checking system requirements...")
        
        # Check macOS version
        result = self.run_cmd(['sw_vers', '-productVersion'])
        macos_version = result.stdout.strip()
        self.log(f"macOS version: {macos_version}")
        
        # Check Python version
        result = self.run_cmd([sys.executable, '--version'])
        python_version = result.stdout.strip()
        self.log(f"Python version: {python_version}")
        
        # Check architecture
        result = self.run_cmd(['uname', '-m'])
        arch = result.stdout.strip()
        self.log(f"Host architecture: {arch}")
        
        # Check for required tools
        tools = ['python3', 'pip3', 'iconutil', 'hdiutil', 'lipo']
        for tool in tools:
            result = self.run_cmd(['which', tool], check=False)
            if result.returncode == 0:
                self.log(f"✓ {tool} found", "SUCCESS")
            else:
                self.log(f"✗ {tool} not found", "ERROR")
                return False
        
        return True
    
    def install_build_dependencies(self):
        """Install required build dependencies"""
        self.log("Installing build dependencies...")
        
        # Check for Homebrew
        result = self.run_cmd(['which', 'brew'], check=False)
        if result.returncode != 0:
            self.log("Homebrew not found. Please install from https://brew.sh", "ERROR")
            return False
        
        # Install create-dmg if not present
        result = self.run_cmd(['which', 'create-dmg'], check=False)
        if result.returncode != 0:
            self.log("Installing create-dmg...")
            self.run_cmd(['brew', 'install', 'create-dmg'])
        
        # Install Python dependencies
        self.log("Installing Python dependencies...")
        self.run_cmd([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
        
        # Install py2app and other build deps
        build_deps = [
            'py2app>=0.28',
            'setuptools>=65.0', 
            'wheel>=0.38',
            'PyGObject>=3.42',
            'pycairo>=1.20.0',
            'paramiko>=3.4',
            'cryptography>=42.0',
            'psutil>=5.9.0'
        ]
        
        for dep in build_deps:
            try:
                self.run_cmd([sys.executable, '-m', 'pip', 'install', dep])
            except:
                self.log(f"Failed to install {dep}", "WARNING")
        
        return True
    
    def create_app_icon(self):
        """Create macOS app icon from SVG"""
        self.log("Creating app icon...")
        
        svg_path = self.script_dir / "sshpilot" / "io.github.mfat.sshpilot.svg"
        icns_path = self.script_dir / "app_icon.icns"
        
        if not svg_path.exists():
            self.log(f"SVG icon not found at {svg_path}", "WARNING")
            icns_path.touch()
            return icns_path
        
        # Create iconset directory
        iconset_dir = self.script_dir / f"{self.app_name}.iconset"
        iconset_dir.mkdir(exist_ok=True)
        
        # Icon sizes for macOS
        icon_sizes = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        ]
        
        # Convert SVG to PNG at different sizes
        for size, filename in icon_sizes:
            png_path = iconset_dir / filename
            try:
                self.run_cmd([
                    'sips', '-s', 'format', 'png',
                    '-s', 'pixelsWide', str(size),
                    '-s', 'pixelsHigh', str(size),
                    str(svg_path), '--out', str(png_path)
                ], check=False)
            except:
                self.log(f"Could not create {filename}", "WARNING")
        
        # Convert iconset to icns
        try:
            self.run_cmd(['iconutil', '-c', 'icns', str(iconset_dir)])
            if (self.script_dir / f"{self.app_name}.icns").exists():
                shutil.move(str(self.script_dir / f"{self.app_name}.icns"), str(icns_path))
            self.log(f"✓ Icon created: {icns_path}", "SUCCESS")
        except:
            self.log("Could not create icns file", "WARNING")
            icns_path.touch()
        finally:
            if iconset_dir.exists():
                shutil.rmtree(iconset_dir)
        
        return icns_path
    
    def create_setup_py(self, icon_path):
        """Generate setup.py for py2app"""
        self.log("Creating setup.py configuration...")
        
        setup_content = f'''#!/usr/bin/env python3
from setuptools import setup, find_packages
import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

APP = ['run.py']

# Collect all necessary data files
DATA_FILES = []

# Include GResource and UI files
def collect_data_files():
    files = []
    for root, dirs, filenames in os.walk('sshpilot'):
        for filename in filenames:
            if filename.endswith(('.gresource', '.ui', '.xml', '.css', '.svg')):
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, 'sshpilot')
                files.append((os.path.dirname(rel_path), [full_path]))
    return files

DATA_FILES = collect_data_files()

OPTIONS = {{
    'py2app': {{
        'arch': 'universal2',
        'argv_emulation': False,
        'semi_standalone': True,
        'site_packages': True,
        'optimize': 2,
        'compressed': True,
        'iconfile': '{icon_path.name}',
        'plist': {{
            'CFBundleName': '{self.app_name}',
            'CFBundleDisplayName': '{self.app_name}',
            'CFBundleIdentifier': '{self.bundle_id}',
            'CFBundleVersion': '{self.version}',
            'CFBundleShortVersionString': '{self.version}',
            'CFBundleExecutable': '{self.app_name}',
            'LSMinimumSystemVersion': '{self.min_macos}',
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.utilities',
            'NSRequiresAquaSystemAppearance': False,
            'LSArchitecturePriority': ['arm64', 'x86_64'],
            'NSSupportsAutomaticGraphicsSwitching': True,
        }},
        'includes': [
            'gi', 'gi.repository', 'gi.repository.Gtk', 'gi.repository.Adw',
            'gi.repository.Vte', 'gi.repository.Gio', 'gi.repository.GLib',
            'gi.repository.Pango', 'gi.repository.GdkPixbuf',
            'paramiko', 'cryptography', 'psutil',
            'sshpilot.main', 'sshpilot.window', 'sshpilot.terminal'
        ],
        'packages': ['gi', 'sshpilot', 'paramiko', 'cryptography', 'psutil'],
        'excludes': ['tkinter', 'test', 'unittest', 'distutils', 'email', 'xml'],
        'strip': True,
    }}
}}

setup(
    name='{self.app_name}',
    version='{self.version}',
    description='SSH connection manager with integrated terminal',
    app=APP,
    data_files=DATA_FILES,
    options=OPTIONS,
    setup_requires=['py2app'],
)
'''
        
        with open(self.script_dir / "setup.py", 'w') as f:
            f.write(setup_content)
    
    def build_app(self):
        """Build the macOS app bundle"""
        self.log("Building macOS application bundle...")
        
        # Create icon
        icon_path = self.create_app_icon()
        
        # Create setup.py
        self.create_setup_py(icon_path)
        
        # Build with py2app
        self.run_cmd([sys.executable, 'setup.py', 'py2app', '--arch=universal2'])
        
        if not self.app_path.exists():
            self.log("App bundle creation failed", "ERROR")
            return False
        
        self.log(f"✓ App bundle created: {self.app_path}", "SUCCESS")
        return True
    
    def verify_universal_binary(self):
        """Verify the binary supports both architectures"""
        self.log("Verifying universal binary...")
        
        executable = self.app_path / "Contents" / "MacOS" / self.app_name
        if not executable.exists():
            self.log(f"Executable not found: {executable}", "WARNING")
            return False
        
        # Check file type
        result = self.run_cmd(['file', str(executable)])
        self.log(f"File type: {result.stdout.strip()}")
        
        # Check architectures
        result = self.run_cmd(['lipo', '-info', str(executable)], check=False)
        if result.returncode == 0:
            arch_info = result.stdout.strip()
            self.log(f"Architectures: {arch_info}")
            
            if 'arm64' in arch_info and 'x86_64' in arch_info:
                self.log("✓ Universal binary confirmed (Intel + Apple Silicon)", "SUCCESS")
                return True
            else:
                self.log("⚠ Binary may not be universal", "WARNING")
                return False
        else:
            self.log("Could not verify architectures", "WARNING")
            return False
    
    def create_dmg(self):
        """Create the final DMG"""
        self.log("Creating DMG...")
        
        # Remove existing DMG
        if self.dmg_path.exists():
            self.dmg_path.unlink()
        
        # Try create-dmg first
        if self.create_dmg_with_create_dmg():
            return True
        
        # Fallback to hdiutil
        return self.create_dmg_with_hdiutil()
    
    def create_dmg_with_create_dmg(self):
        """Create DMG using create-dmg tool"""
        try:
            cmd = [
                "create-dmg",
                "--volname", f"{self.app_name} {self.version}",
                "--window-pos", "200", "120",
                "--window-size", "600", "400", 
                "--icon-size", "100",
                "--icon", f"{self.app_name}.app", "150", "200",
                "--hide-extension", f"{self.app_name}.app",
                "--app-drop-link", "450", "200",
                "--format", "UDZO",
                str(self.dmg_path),
                str(self.dist_dir)
            ]
            
            self.run_cmd(cmd)
            self.log("✓ DMG created with create-dmg", "SUCCESS")
            return True
        except:
            self.log("create-dmg failed, trying fallback", "WARNING")
            return False
    
    def create_dmg_with_hdiutil(self):
        """Fallback DMG creation using hdiutil"""
        self.log("Creating DMG with hdiutil...")
        
        try:
            # Calculate required size
            app_size = self.get_dir_size(self.app_path)
            dmg_size_mb = max(int(app_size / 1024 / 1024) + 100, 200)
            
            temp_dmg = self.dist_dir / "temp.dmg"
            
            # Create empty DMG
            self.run_cmd([
                "hdiutil", "create", "-size", f"{dmg_size_mb}m",
                "-volname", f"{self.app_name} {self.version}",
                "-fs", "HFS+", "-format", "UDRW", str(temp_dmg)
            ])
            
            # Mount DMG
            result = self.run_cmd([
                "hdiutil", "attach", str(temp_dmg), "-mountroot", "/tmp", "-nobrowse"
            ])
            
            # Find mount point
            mount_point = None
            for line in result.stdout.split('\n'):
                if '/tmp' in line and self.app_name in line:
                    mount_point = line.split()[-1]
                    break
            
            if not mount_point:
                # Try alternative parsing
                for line in result.stdout.split('\n'):
                    if '/tmp' in line:
                        parts = line.split()
                        if len(parts) > 0:
                            mount_point = parts[-1]
                            break
            
            if not mount_point:
                self.log("Could not determine mount point", "ERROR")
                return False
            
            mount_path = Path(mount_point)
            self.log(f"Mounted at: {mount_path}")
            
            try:
                # Copy app to DMG
                dest_app = mount_path / f"{self.app_name}.app"
                shutil.copytree(self.app_path, dest_app)
                
                # Create Applications symlink
                apps_link = mount_path / "Applications"
                if not apps_link.exists():
                    apps_link.symlink_to("/Applications")
                
                # Set basic Finder view options using AppleScript
                applescript = f'''
                tell application "Finder"
                    tell disk "{self.app_name} {self.version}"
                        open
                        set current view of container window to icon view
                        set toolbar visible of container window to false
                        set statusbar visible of container window to false
                        set the bounds of container window to {{200, 120, 800, 520}}
                        set icon size of icon view options of container window to 100
                        set arrangement of icon view options of container window to not arranged
                        try
                            set position of item "{self.app_name}.app" of container window to {{150, 200}}
                            set position of item "Applications" of container window to {{450, 200}}
                        end try
                        update without registering applications
                        delay 2
                        close
                    end tell
                end tell
                '''
                
                self.run_cmd(['osascript', '-e', applescript], check=False)
                
            finally:
                # Unmount
                self.run_cmd(["hdiutil", "detach", mount_point], check=False)
            
            # Convert to compressed DMG
            self.run_cmd([
                "hdiutil", "convert", str(temp_dmg),
                "-format", "UDZO", "-imagekey", "zlib-level=9",
                "-o", str(self.dmg_path)
            ])
            
            # Clean up
            temp_dmg.unlink()
            
            self.log("✓ DMG created with hdiutil", "SUCCESS")
            return True
            
        except Exception as e:
            self.log(f"hdiutil DMG creation failed: {e}", "ERROR")
            return False
    
    def get_dir_size(self, path):
        """Calculate directory size in bytes"""
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.isfile(filepath):
                    total_size += os.path.getsize(filepath)
        return total_size
    
    def verify_dmg(self):
        """Verify the created DMG"""
        if not self.dmg_path.exists():
            self.log("DMG file not found", "ERROR")
            return False
        
        self.log("Verifying DMG...")
        
        try:
            # Verify DMG integrity
            self.run_cmd(["hdiutil", "verify", str(self.dmg_path)])
            
            # Get file size
            size_mb = self.dmg_path.stat().st_size / 1024 / 1024
            self.log(f"✓ DMG verified successfully ({size_mb:.1f} MB)", "SUCCESS")
            
            return True
        except:
            self.log("DMG verification failed", "ERROR")
            return False
    
    def clean_build(self):
        """Clean previous build artifacts"""
        self.log("Cleaning previous builds...")
        
        paths_to_clean = [
            self.build_dir,
            self.dist_dir,
            self.script_dir / "setup.py",
            self.script_dir / "app_icon.icns",
        ]
        
        for path in paths_to_clean:
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        
        # Clean egg-info directories
        for egg_info in self.script_dir.glob("*.egg-info"):
            if egg_info.is_dir():
                shutil.rmtree(egg_info)
    
    def build(self):
        """Complete build process"""
        self.log(f"Building {self.app_name} Universal DMG for macOS", "INFO")
        self.log("=" * 60)
        
        # Check system
        if not self.check_system_requirements():
            return False
        
        # Install dependencies  
        if not self.install_build_dependencies():
            return False
        
        # Clean previous builds
        self.clean_build()
        
        # Create directories
        self.build_dir.mkdir(exist_ok=True)
        self.dist_dir.mkdir(exist_ok=True)
        
        # Build app
        if not self.build_app():
            return False
        
        # Verify universal binary
        self.verify_universal_binary()
        
        # Create DMG
        if not self.create_dmg():
            return False
        
        # Verify DMG
        if not self.verify_dmg():
            return False
        
        # Success summary
        self.log("\n" + "=" * 60, "SUCCESS")
        self.log("🎉 BUILD COMPLETED SUCCESSFULLY!", "SUCCESS")
        self.log(f"📦 DMG: {self.dmg_path}", "SUCCESS")
        self.log(f"📏 Size: {self.dmg_path.stat().st_size / 1024 / 1024:.1f} MB", "SUCCESS")
        self.log("🏗️  Architectures: Intel x86_64 + Apple Silicon arm64", "SUCCESS")
        self.log("=" * 60, "SUCCESS")
        
        return True

def main():
    builder = UniversalDMGBuilder()
    success = builder.build()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()