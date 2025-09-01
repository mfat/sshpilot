#!/usr/bin/env python3
import os
import sys
import subprocess

def setup_environment():
    """Set up environment variables for GTK/PyGObject"""
    
    # Get bundle paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_contents = os.path.join(script_dir, '..', '..')
    bundle_res = os.path.join(bundle_contents, 'Resources')
    bundle_lib = os.path.join(bundle_res, 'lib')
    bundle_data = os.path.join(bundle_res, 'share')
    bundle_etc = os.path.join(bundle_res, 'etc')
    
    # Set environment variables
    env_vars = {
        'XDG_CONFIG_DIRS': os.path.join(bundle_etc, 'xdg'),
        'XDG_DATA_DIRS': f'/usr/local/share:{bundle_data}',
        'GTK_DATA_PREFIX': bundle_res,
        'GTK_EXE_PREFIX': bundle_res,
        'GTK_PATH': bundle_res,
        'DYLD_FALLBACK_LIBRARY_PATH': f'/usr/local/lib:{os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")}',
        'GI_TYPELIB_PATH': f'/usr/local/lib/girepository-1.0:{os.environ.get("GI_TYPELIB_PATH", "")}',
        'GTK_ICON_THEME': 'Adwaita',
        'XDG_ICON_THEME': 'Adwaita',
        'GTK_APPLICATION_PREFERS_DARK_THEME': '',
        'GTK_THEME_VARIANT': '',
        'GTK_THEME': '',
        'GTK_USE_PORTAL': '1',
        'GTK_CSD': '1',
    }
    
    # Update environment
    for key, value in env_vars.items():
        os.environ[key] = value

def main():
    """Main launcher function"""
    # Set up environment
    setup_environment()
    
    # Get paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_res = os.path.join(script_dir, '..', 'Resources')
    
    # Add Resources to Python path
    sys.path.insert(0, bundle_res)
    
    # Change to Resources directory
    os.chdir(bundle_res)
    
    # Import and run the application
    try:
        from run import main as app_main
        app_main()
    except Exception as e:
        print(f"Error launching sshPilot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
