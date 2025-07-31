#!/usr/bin/env python3
"""
Test script to verify SSH Manager installation
"""

import sys
import os

def test_imports():
    """Test if all required modules can be imported"""
    print("Testing imports...")
    
    try:
        import gi
        print("✓ PyGObject imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import PyGObject: {e}")
        return False
    
    try:
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        print("✓ GTK4 imported successfully")
    except Exception as e:
        print(f"✗ Failed to import GTK4: {e}")
        return False
    
    try:
        gi.require_version('Vte', '3.91')
        from gi.repository import Vte
        print("✓ Vte.Terminal imported successfully")
    except Exception as e:
        print(f"✗ Failed to import Vte.Terminal: {e}")
        return False
    
    try:
        gi.require_version('Secret', '1')
        from gi.repository import Secret
        print("✓ libsecret imported successfully")
    except Exception as e:
        print(f"✗ Failed to import libsecret: {e}")
        return False
    
    try:
        import paramiko
        print("✓ Paramiko imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import Paramiko: {e}")
        return False
    
    try:
        import cryptography
        print("✓ Cryptography imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import Cryptography: {e}")
        return False
    
    return True

def test_ssh_command():
    """Test if SSH command is available"""
    print("\nTesting SSH command...")
    
    import subprocess
    try:
        result = subprocess.run(['ssh', '-V'], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ SSH command available: {result.stdout.strip()}")
            return True
        else:
            print(f"✗ SSH command failed: {result.stderr}")
            return False
    except FileNotFoundError:
        print("✗ SSH command not found in PATH")
        return False

def test_ssh_keys():
    """Test SSH key detection"""
    print("\nTesting SSH key detection...")
    
    from pathlib import Path
    ssh_dir = Path.home() / ".ssh"
    
    if ssh_dir.exists():
        keys = []
        for key_file in ssh_dir.glob("*"):
            if key_file.is_file() and not key_file.name.endswith('.pub'):
                keys.append(key_file.name)
        
        if keys:
            print(f"✓ Found SSH keys: {', '.join(keys)}")
        else:
            print("⚠ No SSH keys found in ~/.ssh/")
        return True
    else:
        print("⚠ ~/.ssh/ directory not found")
        return True

def test_sshpilot_import():
    """Test if SSHPilot can be imported"""
    print("\nTesting SSHPilot import...")
    
    try:
        # Add current directory to path
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        
        # Import SSHPilot classes
        from sshpilot import SSHConnection
        print("✓ SSHPilot classes imported successfully")
        return True
    except Exception as e:
        print(f"✗ Failed to import SSHPilot: {e}")
        return False

def main():
    """Run all tests"""
    print("SSHPilot - Installation Test")
    print("=" * 40)
    
    tests = [
        test_imports,
        test_ssh_command,
        test_ssh_keys,
        test_sshpilot_import
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print("\n" + "=" * 40)
    print(f"Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("✓ All tests passed! SSHPilot is ready to use.")
        print("\nYou can now run: python3 sshpilot.py")
    else:
        print("✗ Some tests failed. Please check the installation.")
        print("\nTry running: ./install.sh")
    
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 