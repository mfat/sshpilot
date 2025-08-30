#!/usr/bin/env python3
"""
Advanced DMG creation script for sshPilot
Creates a beautifully styled DMG with proper macOS conventions
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

class DMGBuilder:
    def __init__(self):
        self.app_name = "sshPilot"
        self.version = "1.0.0"
        self.bundle_id = "io.github.mfat.sshpilot"
        self.script_dir = Path(__file__).parent.absolute()
        self.dist_dir = self.script_dir / "dist"
        self.app_path = self.dist_dir / f"{self.app_name}.app"
        self.dmg_name = f"{self.app_name}-{self.version}-universal"
        self.dmg_path = self.dist_dir / f"{self.dmg_name}.dmg"
        
    def run_cmd(self, cmd, check=True, cwd=None):
        """Execute command with error handling"""
        print(f"→ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
        result = subprocess.run(cmd, shell=isinstance(cmd, str), check=check,
                              capture_output=True, text=True, cwd=cwd)
        if result.returncode != 0 and check:
            print(f"✗ Command failed: {result.stderr}")
            sys.exit(1)
        return result
    
    def create_dmg_background(self):
        """Create a custom DMG background image"""
        bg_path = self.script_dir / "dmg_background.png"
        
        # Create background using Python PIL if available, otherwise skip
        try:
            from PIL import Image, ImageDraw, ImageFont
            
            # Create a 600x400 background
            img = Image.new('RGB', (600, 400), color='#f8f9fa')
            draw = ImageDraw.Draw(img)
            
            # Try to use a nice font
            try:
                font_large = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 28)
                font_small = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 16)
            except:
                font_large = ImageFont.load_default()
                font_small = ImageFont.load_default()
            
            # Draw text
            draw.text((300, 100), f"Install {self.app_name}", fill='#2c3e50', 
                     font=font_large, anchor='mm')
            draw.text((300, 140), "Drag the app to the Applications folder", 
                     fill='#7f8c8d', font=font_small, anchor='mm')
            
            # Draw arrow or installation hint
            draw.text((300, 320), "← Drag to Applications →", fill='#3498db', 
                     font=font_small, anchor='mm')
            
            img.save(bg_path, 'PNG')
            print(f"✓ Created DMG background: {bg_path}")
            return bg_path
            
        except ImportError:
            print("PIL not available, skipping custom background")
            return None
    
    def create_dmg_with_style(self):
        """Create a styled DMG file"""
        print(f"Creating styled DMG: {self.dmg_path}")
        
        # Remove existing DMG
        if self.dmg_path.exists():
            self.dmg_path.unlink()
        
        # Create background
        bg_path = self.create_dmg_background()
        
        # Prepare create-dmg command
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
            "--filesystem", "HFS+",
        ]
        
        # Add background if created
        if bg_path and bg_path.exists():
            cmd.extend(["--background", str(bg_path)])
        
        # Add volume icon if available
        icon_path = self.script_dir / "app_icon.icns"
        if icon_path.exists():
            cmd.extend(["--volicon", str(icon_path)])
        
        # Add DMG path and source directory
        cmd.extend([str(self.dmg_path), str(self.dist_dir)])
        
        try:
            self.run_cmd(cmd)
            print(f"✓ DMG created successfully: {self.dmg_path}")
            return True
        except:
            print("create-dmg failed, trying fallback method...")
            return self.create_dmg_fallback()
    
    def create_dmg_fallback(self):
        """Fallback DMG creation using hdiutil"""
        print("Creating DMG using hdiutil...")
        
        temp_dmg = self.dist_dir / "temp.dmg"
        
        # Calculate size needed (app size + 50MB buffer)
        app_size = self.get_directory_size(self.app_path)
        dmg_size = max(app_size + 50 * 1024 * 1024, 100 * 1024 * 1024)  # At least 100MB
        dmg_size_mb = int(dmg_size / 1024 / 1024)
        
        # Create writable DMG
        self.run_cmd([
            "hdiutil", "create", "-size", f"{dmg_size_mb}m",
            "-volname", f"{self.app_name} {self.version}",
            "-fs", "HFS+", "-format", "UDRW", str(temp_dmg)
        ])
        
        # Mount the DMG
        mount_result = self.run_cmd([
            "hdiutil", "attach", str(temp_dmg), "-mountroot", "/tmp"
        ])
        
        # Find mount point
        mount_point = None
        for line in mount_result.stdout.split('\n'):
            if self.app_name in line and '/tmp' in line:
                mount_point = line.split()[-1]
                break
        
        if not mount_point:
            print("✗ Could not find mount point")
            return False
        
        mount_path = Path(mount_point)
        
        try:
            # Copy app to DMG
            shutil.copytree(self.app_path, mount_path / f"{self.app_name}.app")
            
            # Create Applications symlink
            apps_link = mount_path / "Applications"
            if not apps_link.exists():
                apps_link.symlink_to("/Applications")
            
            # Set custom icon and layout (basic)
            self.run_cmd([
                "osascript", "-e", f'''
                tell application "Finder"
                    tell disk "{self.app_name} {self.version}"
                        open
                        set current view of container window to icon view
                        set toolbar visible of container window to false
                        set statusbar visible of container window to false
                        set the bounds of container window to {{200, 120, 800, 520}}
                        set arrangement of icon view options of container window to not arranged
                        set icon size of icon view options of container window to 100
                        set position of item "{self.app_name}.app" of container window to {{150, 200}}
                        set position of item "Applications" of container window to {{450, 200}}
                        update without registering applications
                        delay 2
                        close
                    end tell
                end tell
                '''
            ], check=False)
            
        finally:
            # Unmount
            self.run_cmd(["hdiutil", "detach", mount_point])
        
        # Convert to compressed DMG
        self.run_cmd([
            "hdiutil", "convert", str(temp_dmg),
            "-format", "UDZO", "-imagekey", "zlib-level=9",
            "-o", str(self.dmg_path)
        ])
        
        # Clean up
        temp_dmg.unlink()
        
        print(f"✓ DMG created: {self.dmg_path}")
        return True
    
    def get_directory_size(self, path):
        """Get total size of directory"""
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
            print("✗ DMG file not found")
            return False
        
        print("Verifying DMG...")
        try:
            # Verify DMG integrity
            self.run_cmd(["hdiutil", "verify", str(self.dmg_path)])
            
            # Get DMG info
            result = self.run_cmd(["hdiutil", "imageinfo", str(self.dmg_path)])
            print("DMG Information:")
            for line in result.stdout.split('\n'):
                if any(keyword in line.lower() for keyword in ['format', 'size', 'compressed']):
                    print(f"  {line.strip()}")
            
            # Get file size
            size_mb = self.dmg_path.stat().st_size / 1024 / 1024
            print(f"✓ DMG size: {size_mb:.1f} MB")
            
            return True
        except:
            print("✗ DMG verification failed")
            return False
    
    def build(self):
        """Main build process"""
        print(f"Building {self.app_name} DMG for macOS (Universal)")
        print("=" * 60)
        
        # Check if app exists
        if not self.app_path.exists():
            print(f"✗ App not found at {self.app_path}")
            print("Please build the app first using build_macos.py")
            return False
        
        # Create DMG
        if self.create_dmg_with_style():
            if self.verify_dmg():
                print("\n" + "=" * 60)
                print("✓ DMG BUILD SUCCESSFUL")
                print(f"✓ File: {self.dmg_path}")
                print(f"✓ Size: {self.dmg_path.stat().st_size / 1024 / 1024:.1f} MB")
                print("✓ Supports: Intel x86_64 and Apple Silicon arm64")
                print("=" * 60)
                return True
        
        print("✗ DMG build failed")
        return False

def main():
    builder = DMGBuilder()
    success = builder.build()
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()