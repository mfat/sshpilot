"""
Startup information and system diagnostics for sshPilot
"""

import os
import sys
import platform
import shutil
import logging
from pathlib import Path

try:
    import gi
    gi.require_version('Adw', '1')
    gi.require_version('Gtk', '4.0')
    gi.require_version('Vte', '3.91')
    from gi.repository import Adw, Gtk, Vte
    GTK_AVAILABLE = True
except Exception:
    GTK_AVAILABLE = False

try:
    import gi
    gi.require_version('Secret', '1')
    from gi.repository import Secret
    LIBSECRET_AVAILABLE = True
except Exception:
    LIBSECRET_AVAILABLE = False

try:
    import keyring
    KEYRING_AVAILABLE = True
except Exception:
    KEYRING_AVAILABLE = False

from . import __version__
from .platform_utils import is_macos, is_flatpak, get_config_dir, get_ssh_dir


logger = logging.getLogger(__name__)


class StartupInfo:
    """Gather and display startup information"""
    
    # Box drawing characters that work everywhere
    HEADER_LINE = "=" * 60
    SECTION_LINE = "-" * 60
    CHECK_OK = "[OK]"
    CHECK_WARN = "[WARN]"
    CHECK_FAIL = "[FAIL]"
    CHECK_INFO = "[INFO]"
    
    def __init__(self, isolated: bool = False):
        self.isolated = isolated
        self.info = {}
        self._gather_info()
    
    def _gather_info(self):
        """Gather all system information"""
        self.info = {
            'version': self._get_version_info(),
            'platform': self._get_platform_info(),
            'python': self._get_python_info(),
            'libraries': self._get_library_info(),
            'tools': self._get_tools_info(),
            'storage': self._get_storage_info(),
            'config': self._get_config_info(),
        }
    
    def _get_version_info(self):
        """Get application version"""
        return {
            'version': __version__,
        }
    
    def _get_platform_info(self):
        """Get platform information"""
        system = platform.system()
        
        # Get Linux distribution info
        distro_info = ""
        if system == "Linux":
            try:
                # Try to read os-release file
                if os.path.exists("/etc/os-release"):
                    with open("/etc/os-release") as f:
                        for line in f:
                            if line.startswith("PRETTY_NAME="):
                                distro_info = line.split("=", 1)[1].strip().strip('"')
                                break
                if not distro_info and hasattr(platform, 'freedesktop_os_release'):
                    distro_info = platform.freedesktop_os_release().get('PRETTY_NAME', 'Unknown')
            except Exception:
                pass
            
            if not distro_info:
                # Fallback for older Python versions
                try:
                    import distro as distro_module
                    distro_info = distro_module.name(pretty=True)
                except ImportError:
                    distro_info = "Unknown Linux"
        elif system == "Darwin":
            distro_info = f"macOS {platform.mac_ver()[0]}"
        elif system == "Windows":
            distro_info = f"Windows {platform.release()}"
        else:
            distro_info = platform.release()
        
        return {
            'system': system,
            'distro': distro_info,
            'architecture': platform.machine(),
            'flatpak': is_flatpak(),
            'macos': is_macos(),
        }
    
    def _get_python_info(self):
        """Get Python version information"""
        return {
            'version': f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            'implementation': platform.python_implementation(),
        }
    
    def _get_library_info(self):
        """Get library version information"""
        libs = {}
        
        # GTK4
        if GTK_AVAILABLE:
            try:
                gtk_version = f"{Gtk.MAJOR_VERSION}.{Gtk.MINOR_VERSION}.{Gtk.MICRO_VERSION}"
                libs['gtk4'] = {'available': True, 'version': gtk_version}
            except Exception:
                libs['gtk4'] = {'available': False, 'version': None}
        else:
            libs['gtk4'] = {'available': False, 'version': None}
        
        # libadwaita
        if GTK_AVAILABLE:
            try:
                adw_version = f"{Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}.{Adw.MICRO_VERSION}"
                libs['libadwaita'] = {'available': True, 'version': adw_version}
            except Exception:
                libs['libadwaita'] = {'available': False, 'version': None}
        else:
            libs['libadwaita'] = {'available': False, 'version': None}
        
        # VTE
        if GTK_AVAILABLE:
            try:
                vte_version = f"{Vte.MAJOR_VERSION}.{Vte.MINOR_VERSION}.{Vte.MICRO_VERSION}"
                libs['vte'] = {'available': True, 'version': vte_version}
            except Exception:
                libs['vte'] = {'available': False, 'version': None}
        else:
            libs['vte'] = {'available': False, 'version': None}
        
        # PyGObject
        try:
            import gi
            gi_version = gi.__version__ if hasattr(gi, '__version__') else 'unknown'
            libs['pygobject'] = {'available': True, 'version': gi_version}
        except Exception:
            libs['pygobject'] = {'available': False, 'version': None}
        
        # Paramiko
        try:
            import paramiko
            libs['paramiko'] = {'available': True, 'version': paramiko.__version__}
        except Exception:
            libs['paramiko'] = {'available': False, 'version': None}
        
        # Cryptography
        try:
            import cryptography
            libs['cryptography'] = {'available': True, 'version': cryptography.__version__}
        except Exception:
            libs['cryptography'] = {'available': False, 'version': None}
        
        return libs
    
    def _get_tools_info(self):
        """Get information about external tools"""
        tools = {}
        
        # SSH
        ssh_path = shutil.which('ssh')
        if ssh_path:
            try:
                import subprocess
                result = subprocess.run(['ssh', '-V'], capture_output=True, text=True, timeout=2)
                # SSH outputs version to stderr
                version_output = result.stderr.strip() if result.stderr else result.stdout.strip()
                # Extract just the version number (e.g., "OpenSSH_8.9p1")
                version = version_output.split()[0] if version_output else 'unknown'
                tools['ssh'] = {'available': True, 'path': ssh_path, 'version': version}
            except Exception:
                tools['ssh'] = {'available': True, 'path': ssh_path, 'version': 'unknown'}
        else:
            tools['ssh'] = {'available': False, 'path': None, 'version': None}
        
        # sshpass
        sshpass_path = None
        if os.path.exists('/app/bin/sshpass') and os.access('/app/bin/sshpass', os.X_OK):
            sshpass_path = '/app/bin/sshpass'
        else:
            sshpass_path = shutil.which('sshpass')
        
        if sshpass_path:
            try:
                import subprocess
                result = subprocess.run([sshpass_path, '-V'], capture_output=True, text=True, timeout=2)
                version_output = result.stdout.strip() if result.stdout else result.stderr.strip()
                # Extract version (e.g., "1.09")
                version = version_output.split()[1] if len(version_output.split()) > 1 else 'unknown'
                tools['sshpass'] = {'available': True, 'path': sshpass_path, 'version': version, 'executable': True}
            except Exception:
                tools['sshpass'] = {'available': True, 'path': sshpass_path, 'version': 'unknown', 'executable': True}
        else:
            tools['sshpass'] = {'available': False, 'path': None, 'version': None, 'executable': False}
        
        # ssh-askpass
        askpass_path = shutil.which('ssh-askpass')
        tools['ssh_askpass'] = {'available': bool(askpass_path), 'path': askpass_path}
        
        return tools
    
    def _get_storage_info(self):
        """Get secure storage information"""
        storage = {}
        
        # libsecret
        if LIBSECRET_AVAILABLE:
            try:
                # Try to connect to Secret Service
                Secret.Service.get_sync(Secret.ServiceFlags.NONE)
                storage['libsecret'] = {
                    'available': True,
                    'accessible': True,
                    'backend': 'Secret Service (libsecret)'
                }
            except Exception as e:
                storage['libsecret'] = {
                    'available': True,
                    'accessible': False,
                    'error': str(e)
                }
        else:
            storage['libsecret'] = {'available': False, 'accessible': False}
        
        # Keyring
        if KEYRING_AVAILABLE:
            try:
                backend = keyring.get_keyring()
                backend_name = backend.__class__.__name__
                # Check if it's a usable backend (not the fail backend)
                if 'fail' in backend_name.lower() or 'null' in backend_name.lower():
                    storage['keyring'] = {
                        'available': True,
                        'accessible': False,
                        'backend': backend_name
                    }
                else:
                    storage['keyring'] = {
                        'available': True,
                        'accessible': True,
                        'backend': backend_name
                    }
            except Exception as e:
                storage['keyring'] = {
                    'available': True,
                    'accessible': False,
                    'error': str(e)
                }
        else:
            storage['keyring'] = {'available': False, 'accessible': False}
        
        # Determine effective backend
        effective_backend = 'none'
        if not is_macos() and storage.get('libsecret', {}).get('accessible'):
            effective_backend = 'libsecret'
        elif storage.get('keyring', {}).get('accessible'):
            backend_name = storage.get('keyring', {}).get('backend', 'unknown')
            effective_backend = f"keyring ({backend_name})"
        
        storage['effective_backend'] = effective_backend
        
        return storage
    
    def _get_config_info(self):
        """Get configuration information"""
        config_dir = get_config_dir()
        ssh_dir = get_ssh_dir()
        
        # SSH config file location
        if self.isolated:
            ssh_config_file = os.path.join(config_dir, "config")
        else:
            ssh_config_file = os.path.join(ssh_dir, "config")
        
        # App config file location
        app_config_file = os.path.join(config_dir, "config.json")
        
        return {
            'isolated_mode': self.isolated,
            'ssh_config_file': ssh_config_file,
            'ssh_config_exists': os.path.exists(ssh_config_file),
            'app_config_file': app_config_file,
            'app_config_exists': os.path.exists(app_config_file),
            'config_dir': config_dir,
            'ssh_dir': ssh_dir,
        }
    
    def print_info(self):
        """Print startup information in a clean, formatted way"""
        print()
        print(self.HEADER_LINE)
        print(f"  SSH Pilot version {self.info['version']['version']}")
        print(self.HEADER_LINE)
        print()
        sys.stdout.flush()
        
        # Platform Information
        print(f"{self.CHECK_INFO} Platform Information")
        print(self.SECTION_LINE)
        platform_info = self.info['platform']
        print(f"  Operating System: {platform_info['system']} ({platform_info['distro']})")
        print(f"  Architecture: {platform_info['architecture']}")
        print(f"  Flatpak: {'Yes' if platform_info['flatpak'] else 'No'}")
        print()
        
        # Python Information
        print(f"{self.CHECK_INFO} Python Environment")
        print(self.SECTION_LINE)
        python_info = self.info['python']
        print(f"  Python version: {python_info['version']} ({python_info['implementation']})")
        print()
        
        # Library Information
        print(f"{self.CHECK_INFO} Required Libraries")
        print(self.SECTION_LINE)
        libs = self.info['libraries']
        
        for lib_name, lib_info in libs.items():
            if lib_info['available']:
                version_str = f"version {lib_info['version']}" if lib_info['version'] else "version unknown"
                status = self.CHECK_OK
                print(f"  {status} {lib_name}: {version_str}")
            else:
                status = self.CHECK_FAIL
                print(f"  {status} {lib_name}: NOT FOUND")
        print()
        
        # Tools Information
        print(f"{self.CHECK_INFO} External Tools")
        print(self.SECTION_LINE)
        tools = self.info['tools']
        
        # SSH
        if tools['ssh']['available']:
            print(f"  {self.CHECK_OK} ssh: {tools['ssh']['version']} at {tools['ssh']['path']}")
        else:
            print(f"  {self.CHECK_FAIL} ssh: NOT FOUND")
        
        # sshpass
        if tools['sshpass']['available'] and tools['sshpass']['executable']:
            version_str = f"{tools['sshpass']['version']}" if tools['sshpass']['version'] else "unknown version"
            print(f"  {self.CHECK_OK} sshpass: {version_str} at {tools['sshpass']['path']}")
        else:
            print(f"  {self.CHECK_WARN} sshpass: not available (password authentication will be limited)")
        
        # ssh-askpass
        if tools['ssh_askpass']['available']:
            print(f"  {self.CHECK_OK} ssh-askpass: found at {tools['ssh_askpass']['path']}")
        else:
            print(f"  {self.CHECK_INFO} ssh-askpass: not found (will use built-in askpass)")
        print()
        
        # Storage Information
        print(f"{self.CHECK_INFO} Secure Storage")
        print(self.SECTION_LINE)
        storage = self.info['storage']
        
        # Platform-specific storage
        if is_macos():
            keyring_info = storage.get('keyring', {})
            if keyring_info.get('accessible'):
                backend = keyring_info.get('backend', 'unknown')
                print(f"  {self.CHECK_OK} Keyring: accessible (backend: {backend})")
            else:
                print(f"  {self.CHECK_WARN} Keyring: not accessible")
        else:
            # Linux - check libsecret first
            libsecret_info = storage.get('libsecret', {})
            if libsecret_info.get('accessible'):
                print(f"  {self.CHECK_OK} libsecret: accessible via Secret Service")
            elif libsecret_info.get('available'):
                error = libsecret_info.get('error', 'unknown error')
                print(f"  {self.CHECK_WARN} libsecret: available but not accessible ({error})")
            else:
                print(f"  {self.CHECK_WARN} libsecret: not available")
            
            # Fallback to keyring on Linux
            keyring_info = storage.get('keyring', {})
            if keyring_info.get('accessible'):
                backend = keyring_info.get('backend', 'unknown')
                print(f"  {self.CHECK_OK} Keyring: accessible (backend: {backend})")
            elif keyring_info.get('available'):
                backend = keyring_info.get('backend', 'unknown')
                print(f"  {self.CHECK_WARN} Keyring: available but not usable (backend: {backend})")
        
        # Effective backend
        effective = storage.get('effective_backend', 'none')
        if effective == 'none':
            print(f"  {self.CHECK_WARN} Effective backend: none (password storage disabled)")
        else:
            print(f"  {self.CHECK_OK} Effective backend: {effective}")
        print()
        
        # Configuration Information
        print(f"{self.CHECK_INFO} Configuration")
        print(self.SECTION_LINE)
        config = self.info['config']
        print(f"  Isolated mode: {'Yes' if config['isolated_mode'] else 'No'}")
        print(f"  SSH config file: {config['ssh_config_file']}")
        if config['ssh_config_exists']:
            print(f"    Status: {self.CHECK_OK} exists")
        else:
            print(f"    Status: {self.CHECK_INFO} will be created on first use")
        print(f"  App config file: {config['app_config_file']}")
        if config['app_config_exists']:
            print(f"    Status: {self.CHECK_OK} exists")
        else:
            print(f"    Status: {self.CHECK_INFO} will be created on first use")
        print(f"  Config directory: {config['config_dir']}")
        print(f"  SSH directory: {config['ssh_dir']}")
        print()
        
        print(self.HEADER_LINE)
        print()
        sys.stdout.flush()
    
    def log_info(self):
        """Log startup information to logger"""
        logger.info("=" * 60)
        logger.info(f"SSH Pilot version {self.info['version']['version']}")
        logger.info("=" * 60)
        
        platform_info = self.info['platform']
        logger.info(f"Platform: {platform_info['system']} ({platform_info['distro']})")
        logger.info(f"Architecture: {platform_info['architecture']}")
        logger.info(f"Flatpak: {'Yes' if platform_info['flatpak'] else 'No'}")
        
        python_info = self.info['python']
        logger.info(f"Python: {python_info['version']} ({python_info['implementation']})")
        
        # Log critical library status
        libs = self.info['libraries']
        for lib_name, lib_info in libs.items():
            if lib_info['available']:
                logger.debug(f"{lib_name}: {lib_info['version']}")
            else:
                logger.warning(f"{lib_name}: NOT FOUND")
        
        # Log tool availability
        tools = self.info['tools']
        if tools['sshpass']['available']:
            logger.info(f"sshpass: available at {tools['sshpass']['path']}")
        else:
            logger.warning("sshpass: not available")
        
        # Log storage status
        storage = self.info['storage']
        effective = storage.get('effective_backend', 'none')
        logger.info(f"Secure storage backend: {effective}")
        
        # Log config info
        config = self.info['config']
        logger.info(f"Isolated mode: {'Yes' if config['isolated_mode'] else 'No'}")
        logger.info(f"SSH config file: {config['ssh_config_file']}")
        logger.info(f"App config file: {config['app_config_file']}")
        
        logger.info("=" * 60)


def print_startup_info(isolated: bool = False, verbose: bool = False):
    """
    Print startup information to console
    
    Args:
        isolated: Whether running in isolated mode
        verbose: Whether to print full details (verbose only affects other logging, not startup info)
    """
    info = StartupInfo(isolated=isolated)
    
    # Always print the clean formatted output to console
    info.print_info()

