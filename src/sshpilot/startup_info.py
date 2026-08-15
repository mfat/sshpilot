"""
Startup information and system diagnostics for sshPilot
"""

import os
import sys
import platform
import shutil
import logging
from typing import Optional

try:
    import gi
    gi.require_version('Adw', '1')
    gi.require_version('Gtk', '4.0')
    gi.require_version('Vte', '3.91')
    from gi.repository import Adw, Gtk, Vte
    GTK_AVAILABLE = True
except Exception:
    GTK_AVAILABLE = False

# Secret backends are daemon-owned. Storage diagnostics read metadata through the
# daemon secrets API when a client/controller is reachable and otherwise report the
# backends unavailable — this module never imports secret_storage and never
# instantiates a local SecretManager (the legacy libsecret/keyring probe is gone).

from . import __version__
from .platform_utils import is_macos, is_flatpak, get_sshpass_path


logger = logging.getLogger(__name__)

# Controller-style vs client-style method names for each daemon secret metadata read.
_DAEMON_READ_METHODS = {
    "state": ("load_state", "get_secret_state"),
    "registry": ("load_registry", "get_secret_backends"),
    "configuration": ("load_configuration", "get_secret_configuration"),
}


def _daemon_read(reader, name):
    """One metadata read through a controller (``load_*``) or client (``get_secret_*``).

    Returns ``None`` when the reader lacks the method or the daemon errors out, so
    diagnostics degrade to "unavailable" instead of failing the whole bundle."""
    for attr in _DAEMON_READ_METHODS.get(name, ()):
        fn = getattr(reader, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                return None
    return None


class StartupInfo:
    """Gather and display startup information"""
    
    # Box drawing characters that work everywhere
    HEADER_LINE = "=" * 60
    SECTION_LINE = "-" * 60
    CHECK_OK = "[OK]"
    CHECK_WARN = "[WARN]"
    CHECK_FAIL = "[FAIL]"
    CHECK_INFO = "[INFO]"
    
    def __init__(self, isolated: bool = False, verbose: bool = False, config=None,
                 confirmed_mode=None):
        self.isolated = isolated
        self.confirmed_mode = confirmed_mode
        # The concise (non-verbose) summary only reads the SSH version and the
        # storage backend label, so the extra tool/keyring probes are gathered
        # only when they'll actually be printed — they otherwise cost subprocess
        # spawns and Secret Service D-Bus connects for nothing.
        self.verbose = verbose
        self._config = config
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
        
        # sshpass — warm the cache and record availability. The version string
        # is only shown in the verbose dump, so skip the `sshpass -V` spawn on
        # the default path.
        sshpass_path = get_sshpass_path()

        if sshpass_path:
            version = 'unknown'
            if self.verbose:
                try:
                    import subprocess
                    result = subprocess.run([sshpass_path, '-V'], capture_output=True, text=True, timeout=2)
                    version_output = result.stdout.strip() if result.stdout else result.stderr.strip()
                    # Extract version (e.g., "1.09")
                    version = version_output.split()[1] if len(version_output.split()) > 1 else 'unknown'
                except Exception:
                    version = 'unknown'
            tools['sshpass'] = {'available': True, 'path': sshpass_path, 'version': version, 'executable': True}
        else:
            tools['sshpass'] = {'available': False, 'path': None, 'version': None, 'executable': False}
        
        # ssh-askpass
        askpass_path = shutil.which('ssh-askpass')
        tools['ssh_askpass'] = {'available': bool(askpass_path), 'path': askpass_path}
        
        return tools
    
    def _get_storage_info(self):
        """Get secure storage information (daemon-owned).

        Secret backends are owned by the daemon. When a daemon client or controller
        is reachable through ``config``, metadata (registry, lock state,
        configuration) is read through the secrets API; otherwise every backend is
        reported unavailable. This module never imports ``secret_storage`` and never
        instantiates a local ``SecretManager``.
        """
        reader = self._daemon_secrets_reader()
        if reader is not None:
            return self._storage_info_from_daemon(reader)
        return self._storage_info_unavailable()

    def _daemon_secrets_reader(self):
        """The daemon secrets accessor reachable from ``config``, or ``None``.

        A controller (``config.secrets_controller``) is used directly; a raw daemon
        client (``config.client``) is used only when it advertises the secrets
        capability. ``None`` means no daemon is reachable — storage is then reported
        unavailable rather than probed through a local SecretManager.
        """
        source = self._config
        if source is None:
            return None
        controller = getattr(source, "secrets_controller", None)
        if controller is not None:
            return controller
        client = getattr(source, "client", None)
        if client is None:
            return None
        try:
            from sshpilot.api.capabilities import Capability
            capabilities = client.get_capabilities()
        except Exception:
            return None
        if not capabilities.supports(Capability.SECRETS_READ):
            return None
        return client

    def _storage_info_from_daemon(self, reader):
        """Storage metadata read through the daemon secrets API."""
        state = _daemon_read(reader, "state")
        registry = _daemon_read(reader, "registry")
        configuration = _daemon_read(reader, "configuration")

        storage = {
            'libsecret': {'available': None, 'accessible': False},
            'keyring': {'available': None, 'accessible': False},
        }
        if registry is not None:
            storage['available_backends'] = [
                getattr(backend, "name", "")
                for backend in getattr(registry, "backends", ())
                if getattr(backend, "available", False)
            ]
            storage['selected_backend'] = getattr(registry, "selected_backend", "none")
        else:
            storage['available_backends'] = []
            storage['selected_backend'] = 'none'
        if state is not None:
            storage['effective_backend'] = getattr(state, "effective_backend", "none")
            storage['session_locked'] = bool(getattr(state, "needs_unlock", False))
        else:
            storage['effective_backend'] = 'none'
            storage['session_locked'] = False
        if configuration is not None:
            storage['backend'] = getattr(configuration, "backend", None)
            storage['session_timeout'] = getattr(configuration, "session_timeout", None)
            storage['remember_in_keyring'] = getattr(configuration, "remember_in_keyring", None)
        return storage

    def _storage_info_unavailable(self):
        """Storage metadata when no daemon is reachable — never a local manager."""
        return {
            'libsecret': {'available': None, 'accessible': False},
            'keyring': {'available': None, 'accessible': False},
            'effective_backend': 'none',
            'available_backends': [],
            'selected_backend': 'none',
            'session_locked': False,
        }
    
    def _get_config_info(self):
        """Get configuration information"""
        mode = getattr(self.confirmed_mode, "value", self.confirmed_mode)
        return {
            'operation_mode': mode or 'unavailable',
            'config_authority': 'daemon',
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
        print(f"  Operation mode: {config['operation_mode']}")
        print(f"  SSH configuration authority: {config['config_authority']}")
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
        logger.info("Operation mode: %s (authority: %s)",
                    config['operation_mode'], config['config_authority'])
        
        logger.info("=" * 60)


def print_startup_info(isolated: bool = False, verbose: bool = False, config=None,
                       confirmed_mode=None):
    """
    Emit startup information.

    Args:
        isolated: Whether running in isolated mode.
        verbose: When True, dump the full platform/library/path diagnostic
            (~40 lines) to stdout — useful for bug reports. When False, only
            log a single-line summary at INFO so default startup output stays
            concise. Re-run with ``--verbose`` to get the full diagnostic.
        config: Existing Config instance retained for non-SSH diagnostics.
        confirmed_mode: Daemon-confirmed semantic operation mode, if ready.
    """
    info = StartupInfo(
        isolated=isolated,
        verbose=verbose,
        config=config,
        confirmed_mode=confirmed_mode,
    )

    if verbose:
        info.print_info()
        return

    # Concise default: a small, multi-line block at INFO level. Still ~5
    # lines vs the 40-line full dump, but rich enough that bug reports and
    # day-to-day output carry meaningful context.
    def _get(*path, default=None):
        cur = info.info
        for k in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
            if cur is None:
                return default
        return cur

    version_str = _get('version', 'version', default='?')
    logger.info("sshPilot %s starting", version_str)

    # OS line — Linux (Ubuntu 26.04 LTS) x86_64; flag Flatpak when relevant.
    os_name = _get('platform', 'system') or '?'
    distro = _get('platform', 'distro')
    arch = _get('platform', 'architecture') or '?'
    os_bits = [os_name]
    if distro and distro != os_name:
        os_bits[-1] = f"{os_name} ({distro})"
    os_bits.append(arch)
    if _get('platform', 'flatpak'):
        os_bits.append("Flatpak")
    logger.info("  OS:        %s", " ".join(os_bits))

    # Python + GUI stack on one line, so the runtime versions are easy to
    # cross-reference against a bug report.
    py_ver = _get('python', 'version') or '?'
    py_impl = _get('python', 'implementation') or 'CPython'
    libs = info.info.get('libraries') or {}
    def _lib(key: str) -> Optional[str]:
        entry = libs.get(key)
        return entry.get('version') if isinstance(entry, dict) else None
    gtk_ver = _lib('gtk4')
    adw_ver = _lib('libadwaita')
    vte_ver = _lib('vte')
    gui_bits = []
    if gtk_ver:
        gui_bits.append(f"GTK {gtk_ver}")
    if adw_ver:
        gui_bits.append(f"libadwaita {adw_ver}")
    if vte_ver:
        gui_bits.append(f"VTE {vte_ver}")
    gui_str = " / ".join(gui_bits) if gui_bits else "—"
    logger.info("  Runtime:   Python %s (%s) · %s", py_ver, py_impl, gui_str)

    # Secure storage + SSH binary in one line — these are the two things
    # that most commonly explain "why didn't this connection work".
    backend = _get('storage', 'effective_backend') or 'none'
    ssh_path = _get('tools', 'ssh', 'path') or _get('tools', 'ssh') or '—'
    ssh_ver = _get('tools', 'ssh', 'version')
    ssh_str = f"{ssh_path}" if not ssh_ver else f"{ssh_path} ({ssh_ver})"
    logger.info("  Storage:   %s · SSH: %s", backend, ssh_str)

    # Only call out non-default modes so happy-path startup stays clean.
    logger.info("  Mode:      %s (authority: daemon)",
                _get('config', 'operation_mode', default='unavailable'))
