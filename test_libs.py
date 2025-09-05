#!/usr/bin/env python3
import sys
import os

print("=== Library Verification ===")
print(f"Python executable: {sys.executable}")
print(f"Python version: {sys.version}")
print(f"Python path (first 3): {sys.path[:3]}")

# Check environment variables
print(f"\n=== Environment Variables ===")
print(f"GI_TYPELIB_PATH: {os.environ.get('GI_TYPELIB_PATH', 'Not set')}")
print(f"DYLD_LIBRARY_PATH: {os.environ.get('DYLD_LIBRARY_PATH', 'Not set')}")
print(f"PYTHONPATH: {os.environ.get('PYTHONPATH', 'Not set')}")

# Test GTK import
try:
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    print(f"\n=== GTK Info ===")
    print(f"GTK version: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
    
    # Check which GTK library is being used
    import ctypes
    gtk_lib = ctypes.CDLL(None)
    print(f"GTK library loaded successfully")
    
except Exception as e:
    print(f"GTK import failed: {e}")

# Test VTE import
try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
    print(f"\n=== VTE Info ===")
    print(f"VTE version: {Vte.get_major_version()}.{Vte.get_minor_version()}.{Vte.get_micro_version()}")
    print(f"VTE library loaded successfully")
except Exception as e:
    print(f"VTE import failed: {e}")

print("\n=== Test Complete ===")
