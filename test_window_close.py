#!/usr/bin/env python3
"""
Simple test to verify window close functionality
"""

import sys
import os
import time
import subprocess

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_window_close():
    """Test window close functionality"""
    print("=== Window Close Test ===")
    
    # Start the application in background
    print("1. Starting sshPilot...")
    process = subprocess.Popen([
        sys.executable, './run_sshpilot.py'
    ], env={**os.environ, 'SSHPILOT_DEBUG': '1'})
    
    print(f"2. Application started with PID: {process.pid}")
    print("3. Application should be visible now.")
    print("4. Click the window close button (X) to test...")
    print("5. Waiting for process to end...")
    
    try:
        # Wait for the process to end (with timeout)
        return_code = process.wait(timeout=30)
        print(f"6. Process ended with return code: {return_code}")
        
        if return_code == 0:
            print("✅ SUCCESS: Window close worked correctly!")
        else:
            print(f"⚠️  WARNING: Process ended with non-zero code: {return_code}")
            
    except subprocess.TimeoutExpired:
        print("⏰ TIMEOUT: Process didn't end within 30 seconds")
        print("   This suggests the window close button isn't working")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return False
    
    return True

if __name__ == '__main__':
    test_window_close()