#!/usr/bin/env python3
"""
Debug script to test GResource loading
"""

import os
import sys
from gi.repository import Gio, GLib

def debug_load_resources():
    # Simplified lookup: prefer installed site-packages path, with one system fallback.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    possible_paths = [
        os.path.join(current_dir, 'resources', 'sshpilot.gresource'),
        '/usr/share/io.github.mfat.sshpilot/io.github.mfat.sshpilot.gresource',
    ]

    print(f"DEBUG: current_dir: {current_dir}")
    print(f"DEBUG: __file__: {__file__}")
    
    for i, path in enumerate(possible_paths):
        print(f"DEBUG: Checking path {i+1}: {path}")
        print(f"DEBUG: Path exists: {os.path.exists(path)}")
        if os.path.exists(path):
            try:
                resource = Gio.Resource.load(path)
                Gio.resources_register(resource)
                print(f"SUCCESS: Loaded resources from: {path}")
                return True
            except GLib.Error as e:
                print(f"ERROR: Failed to load resources from {path}: {e}")
    print("ERROR: Could not load GResource bundle")
    return False

if __name__ == "__main__":
    debug_load_resources()
