#!/usr/bin/env python3
"""
Run script for sshPilot development
"""

import sys
import os

# Add src directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import and run the application
from sshpilot_pkg.github.mfat.sshpilot.main import main

if __name__ == '__main__':
    main()