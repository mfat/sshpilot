#!/usr/bin/env python3
import os
import sys
import subprocess

def setup_environment():
    """Set up environment variables for GTK/PyGObject"""

    # Get bundle paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_contents = os.path.dirname(script_dir)
    bundle_res = os.path.join(bundle_contents, 'Resources')
    bundle_lib = os.path.join(bundle_res, 'lib')
    bundle_data = os.path.join(bundle_res, 'share')
    bundle_etc = os.path.join(bundle_res, 'etc')

    # Get Homebrew prefix (fallback to common locations)
    brew_prefix = os.environ.get('HOMEBREW_PREFIX', '/usr/local')
    if not os.path.exists(brew_prefix):
        # Try common Homebrew locations
        for prefix in ['/opt/homebrew', '/usr/local']:
            if os.path.exists(prefix):
                brew_prefix = prefix
                break

    # Find the correct Python with PyGObject
    python_paths = [
        '/Library/Frameworks/Python.framework/Versions/3.13/bin/python3',
        '/usr/local/bin/python3',
        '/opt/homebrew/bin/python3',
        '/usr/bin/python3'
    ]

    python_executable = None
    for path in python_paths:
        if os.path.exists(path):
            # Check if this Python has PyGObject
            try:
                result = subprocess.run([path, '-c', 'import gi; print("PyGObject found")'],
                                       capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    python_executable = path
                    break
            except:
                continue

    if not python_executable:
        print("Error: Could not find Python with PyGObject")
        sys.exit(1)

    # Set environment variables with fallbacks
    env_vars = {
        'PATH': f'{brew_prefix}/bin:/usr/bin:/bin',
        'XDG_CONFIG_DIRS': os.path.join(bundle_etc, 'xdg'),
        'XDG_DATA_DIRS': f'{brew_prefix}/share:{bundle_data}',
        'GTK_DATA_PREFIX': bundle_res,
        'GTK_EXE_PREFIX': bundle_res,
        'GTK_PATH': bundle_res,
        'DYLD_FALLBACK_LIBRARY_PATH': f'{brew_prefix}/lib:/usr/lib',
        'GI_TYPELIB_PATH': f'{brew_prefix}/lib/girepository-1.0',
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

    # Ensure Python can find PyGObject
    python_site_packages = None
    for python_version in ['3.13', '3.11', '3.10', '3.9', '3.8']:
        potential_path = f'{brew_prefix}/lib/python{python_version}/site-packages'
        if os.path.exists(potential_path):
            python_site_packages = potential_path
            break

    if python_site_packages:
        os.environ['PYTHONPATH'] = python_site_packages

    return python_executable

def main():
    """Main launcher function"""
    # Set up environment
    python_executable = setup_environment()

    # Get paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundle_res = os.path.join(script_dir, '..', 'Resources')
    app_dir = os.path.join(bundle_res, 'app')

    # Change to the Resources directory
    os.chdir(bundle_res)

    # Launch the application using subprocess to avoid PyGObject circular import issues
    try:
        # Use subprocess to run the app as a module
        # This allows relative imports to work properly
        result = subprocess.run([
            python_executable, '-m', 'app.main'
        ], env=os.environ, cwd=bundle_res)

        if result.returncode != 0:
            print(f"Application exited with code: {result.returncode}")
            sys.exit(result.returncode)

    except Exception as e:
        print(f"Error launching sshPilot: {e}")
        print(f"Python executable: {python_executable}")
        print(f"Working directory: {bundle_res}")
        print(f"Environment:")
        for key in ['PATH', 'PYTHONPATH', 'GI_TYPELIB_PATH', 'DYLD_FALLBACK_LIBRARY_PATH']:
            print(f"  {key}: {os.environ.get(key, 'NOT SET')}")
        sys.exit(1)

if __name__ == "__main__":
    main()
