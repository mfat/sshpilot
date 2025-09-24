#!/usr/bin/env python3
"""
Simple development runner for sshPilot with hot reloading
Usage: python dev.py [options]
"""

import os
import sys
import subprocess
from pathlib import Path

def check_dependencies():
    """Check if required dependencies are installed"""
    try:
        import watchdog
        return True
    except ImportError:
        return False

def install_dependencies():
    """Install required dependencies"""
    print("📦 Installing required dependencies...")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'watchdog'])
        print("✅ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install dependencies: {e}")
        return False

def main():
    """Main entry point"""
    print("🔧 sshPilot Development Mode")
    print("=" * 40)
    
    # Check if we're in the right directory
    if not os.path.exists('run.py'):
        print("❌ Error: run.py not found. Please run this script from the sshPilot root directory")
        sys.exit(1)
    
    # Check dependencies
    if not check_dependencies():
        print("⚠️  Required dependency 'watchdog' not found")
        response = input("Would you like to install it now? (y/N): ").strip().lower()
        if response in ['y', 'yes']:
            if not install_dependencies():
                sys.exit(1)
        else:
            print("❌ Cannot run without watchdog. Exiting.")
            sys.exit(1)
    
    # Run the development runner
    try:
        from dev_runner import SshPilotDevRunner
        runner = SshPilotDevRunner(verbose=True)
        runner.run()
    except ImportError as e:
        print(f"❌ Error importing dev_runner: {e}")
        print("Make sure dev_runner.py is in the same directory")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
