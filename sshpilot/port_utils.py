"""
Port utilities for sshPilot
Provides port information and availability checking functionality
"""

import socket
import subprocess
import logging
from typing import Dict, Iterable, Iterator, List, Optional, Tuple, Any
import json
import re
from gettext import gettext as _

# Try to import psutil, fallback to subprocess methods if not available
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSH port-forwarding rule model & formatting
# ---------------------------------------------------------------------------
#
# A connection's forwarding rules live on ``Connection.forwarding_rules``
# (``connection_manager.py``) as a list of plain dicts. They are parsed from
# ``~/.ssh/config`` LocalForward / RemoteForward / DynamicForward directives in
# ``connection_manager.py`` (see the ``forwarding_rules.append(...)`` blocks
# around lines 1286–1347) and edited via the Port Forwarding page of
# ``connection_dialog.py``.
#
# Every rule is a dict with a ``type`` and an ``enabled`` flag. The remaining
# keys depend on the type:
#
#   local   – forward a local listening port to a host reachable from the server
#     {'type': 'local',   'enabled': bool,
#      'listen_addr': str,   # local bind address (e.g. 'localhost')
#      'listen_port': int,   # local port we listen on
#      'remote_host': str,   # destination host (resolved on the server)
#      'remote_port': int}   # destination port
#
#   remote  – forward a port listening on the server back to a local destination
#     {'type': 'remote',  'enabled': bool,
#      'listen_addr': str,   # bind address on the server (e.g. 'localhost')
#      'listen_port': int,   # port the server listens on
#      # destination, on the local side. Newer rules use local_*, but some
#      # older/imported rules carry remote_* — read with a fallback.
#      'local_host': str,    'local_port': int,
#      'socks': bool}        # single-argument RemoteForward → acts as SOCKS
#
#   dynamic – SOCKS proxy (DynamicForward)
#     {'type': 'dynamic',  'enabled': bool,
#      'listen_addr': str,   # local bind address
#      'listen_port': int}   # local SOCKS port
#
# The helpers below are UI-agnostic (no GTK import) so they can back the sidebar
# indicators, a future port-mapping viewer, tooltips, exports, etc. Strings are
# translatable via gettext.

#: Forwarding ``type`` → the single-letter badge used by the sidebar indicators.
FORWARDING_TYPE_BADGES: Dict[str, str] = {
    "local": "L",
    "remote": "R",
    "dynamic": "D",
}


def iter_enabled_forwarding_rules(rules: Optional[Iterable[Dict[str, Any]]]) -> Iterator[Dict[str, Any]]:
    """Yield the enabled rules from a ``forwarding_rules`` list.

    A rule is considered enabled unless it carries ``'enabled': False`` (the key
    is optional and defaults to enabled). Accepts ``None`` for convenience.
    """
    for rule in rules or []:
        if rule.get("enabled", True):
            yield rule


def group_forwarding_rules(
    rules: Optional[Iterable[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Group enabled rules by type.

    Returns a dict with ``'local'``, ``'remote'`` and ``'dynamic'`` keys, each
    mapping to a list of rule dicts (empty if none). Unknown types are ignored.
    Handy for anything that renders rules per-type (sidebar badges, a viewer's
    sections, etc.).
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {"local": [], "remote": [], "dynamic": []}
    for rule in iter_enabled_forwarding_rules(rules):
        bucket = grouped.get(rule.get("type"))
        if bucket is not None:
            bucket.append(rule)
    return grouped


def format_forwarding_rule(rule: Dict[str, Any]) -> str:
    """Format a single forwarding rule as a human-readable one-line string.

    Examples::

        Local 8080 → localhost:80
        Remote localhost:2222 → 10.0.0.5:22
        Remote localhost:1080 → SOCKS
        SOCKS proxy on port 1080

    Mirrors the descriptions shown on the Port Forwarding page of
    ``connection_dialog.py`` so the wording stays consistent across the app.
    Returns an empty string for an unrecognised ``type``. The output is a
    display label, not a command-line spec — do not feed it to ssh.
    """
    rule_type = rule.get("type")
    if rule_type == "local":
        return _("Local {lp} → {rh}:{rp}").format(
            lp=rule.get("listen_port", ""),
            rh=rule.get("remote_host", ""),
            rp=rule.get("remote_port", ""),
        )
    if rule_type == "remote":
        listen_addr = rule.get("listen_addr") or ""
        listen_port = rule.get("listen_port", "")
        src = f"{listen_addr}:{listen_port}" if listen_addr else f"{listen_port}"
        if rule.get("socks"):
            return _("Remote {src} → SOCKS").format(src=src)
        return _("Remote {src} → {dh}:{dp}").format(
            src=src,
            dh=rule.get("local_host") or rule.get("remote_host", ""),
            dp=rule.get("local_port") or rule.get("remote_port", ""),
        )
    if rule_type == "dynamic":
        return _("SOCKS proxy on port {p}").format(p=rule.get("listen_port", ""))
    return ""


def format_forwarding_rules(
    rules: Optional[Iterable[Dict[str, Any]]],
    *,
    max_lines: Optional[int] = None,
) -> List[str]:
    """Format every enabled rule, skipping blanks (unknown types).

    With ``max_lines`` set, the list is truncated and a final ``… +N more`` line
    is appended when there are more rules than the limit — useful for tooltips
    that must stay compact.
    """
    lines = [line for line in (format_forwarding_rule(r) for r in iter_enabled_forwarding_rules(rules)) if line]
    if max_lines is not None and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [_("… +{n} more").format(n=hidden)]
    return lines


class PortInfo:
    """Information about a port and its usage"""
    
    def __init__(self, port: int, protocol: str = 'tcp', pid: Optional[int] = None, 
                 process_name: Optional[str] = None, address: str = '0.0.0.0'):
        self.port = port
        self.protocol = protocol.lower()
        self.pid = pid
        self.process_name = process_name
        self.address = address
    
    def __str__(self) -> str:
        if self.process_name and self.pid:
            return f"{self.address}:{self.port}/{self.protocol} - {self.process_name} (PID: {self.pid})"
        return f"{self.address}:{self.port}/{self.protocol}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'port': self.port,
            'protocol': self.protocol,
            'pid': self.pid,
            'process_name': self.process_name,
            'address': self.address
        }

class PortChecker:
    """Port availability and information checker"""
    
    def __init__(self):
        self._cache: Dict[str, List[PortInfo]] = {}
        self._cache_timeout = 5  # seconds
        self._last_update = 0
    
    def is_port_available(self, port: int, address: str = '127.0.0.1', protocol: str = 'tcp') -> bool:
        """
        Check if a port is available for binding
        
        Args:
            port: Port number to check
            address: Address to bind to (default: localhost)
            protocol: Protocol (tcp/udp)
            
        Returns:
            True if port is available, False otherwise
        """
        try:
            if protocol.lower() == 'tcp':
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    result = sock.connect_ex((address, port))
                    return result != 0  # 0 means connection successful (port in use)
            else:  # UDP
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    try:
                        sock.bind((address, port))
                        return True
                    except OSError:
                        return False
        except Exception as e:
            logger.debug(f"Error checking port {port}: {e}")
            return False
    
    def get_listening_ports(self, refresh: bool = False) -> List[PortInfo]:
        """
        Get all currently listening ports
        
        Args:
            refresh: Force refresh of port information
            
        Returns:
            List of PortInfo objects for listening ports
        """
        import time
        current_time = time.time()
        
        # Use cache if recent and not forced refresh
        if not refresh and 'listening' in self._cache and \
           (current_time - self._last_update) < self._cache_timeout:
            return self._cache['listening']
        
        ports = []
        
        try:
            if HAS_PSUTIL:
                # Use psutil for cross-platform port information
                connections = psutil.net_connections(kind='inet')
                
                for conn in connections:
                    if conn.status == psutil.CONN_LISTEN and conn.laddr:
                        port_info = PortInfo(
                            port=conn.laddr.port,
                            protocol='tcp' if conn.type == socket.SOCK_STREAM else 'udp',
                            pid=conn.pid,
                            address=conn.laddr.ip
                        )
                        
                        # Get process name if available
                        if conn.pid:
                            try:
                                process = psutil.Process(conn.pid)
                                port_info.process_name = process.name()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        
                        ports.append(port_info)
            else:
                # Fallback to netstat if psutil not available
                ports = self._get_ports_via_netstat()
            
        except Exception as e:
            logger.warning(f"Failed to get port information: {e}")
            # Final fallback to netstat
            ports = self._get_ports_via_netstat()
        
        # Cache results
        self._cache['listening'] = ports
        self._last_update = current_time
        
        return ports
    
    def _get_ports_via_netstat(self) -> List[PortInfo]:
        """Fallback method using netstat command or /proc/net/tcp parsing"""
        ports = []
        
        try:
            # Try netstat command first
            result = subprocess.run(['netstat', '-tlnp'], 
                                  capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'LISTEN' in line:
                        parts = line.split()
                        if len(parts) >= 4:
                            addr_port = parts[3]
                            if ':' in addr_port:
                                addr, port_str = addr_port.rsplit(':', 1)
                                try:
                                    port = int(port_str)
                                    port_info = PortInfo(port=port, address=addr, protocol='tcp')
                                    
                                    # Extract PID/process if available
                                    if len(parts) >= 7 and '/' in parts[6]:
                                        pid_proc = parts[6].split('/', 1)
                                        try:
                                            port_info.pid = int(pid_proc[0])
                                            port_info.process_name = pid_proc[1]
                                        except (ValueError, IndexError):
                                            pass
                                    
                                    ports.append(port_info)
                                except ValueError:
                                    continue
            
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError) as e:
            logger.debug(f"netstat fallback failed: {e}")
            
            # If netstat failed, try parsing /proc/net/tcp directly
            try:
                ports = self._get_ports_via_proc()
            except Exception as proc_e:
                logger.debug(f"/proc/net/tcp parsing failed: {proc_e}")
        
        return ports
    
    def _get_ports_via_proc(self) -> List[PortInfo]:
        """Parse /proc/net/tcp and /proc/net/tcp6 for listening ports"""
        ports = []
        
        # Parse TCP v4 and v6
        for proc_file, is_ipv6 in [('/proc/net/tcp', False), ('/proc/net/tcp6', True)]:
            try:
                with open(proc_file, 'r') as f:
                    lines = f.readlines()[1:]  # Skip header
                    
                    for line in lines:
                        parts = line.strip().split()
                        if len(parts) < 10:
                            continue
                            
                        # Parse local address and port
                        local_addr_port = parts[1]
                        if ':' not in local_addr_port:
                            continue
                            
                        addr_hex, port_hex = local_addr_port.split(':')
                        
                        # Convert port from hex
                        port = int(port_hex, 16)
                        
                        # Convert address from hex
                        if is_ipv6:
                            # IPv6 address parsing (simplified)
                            if addr_hex == '00000000000000000000000000000000':
                                address = '::'
                            else:
                                address = 'IPv6'  # Simplified for now
                        else:
                            # IPv4 address parsing
                            if len(addr_hex) == 8:
                                # Convert little-endian hex to IP
                                addr_int = int(addr_hex, 16)
                                address = f"{addr_int & 0xFF}.{(addr_int >> 8) & 0xFF}.{(addr_int >> 16) & 0xFF}.{(addr_int >> 24) & 0xFF}"
                            else:
                                address = "0.0.0.0"
                        
                        # Check connection state (0A = LISTEN in hex)
                        state = parts[3]
                        if state != '0A':
                            continue
                        
                        # Try to get inode for process lookup
                        inode = None
                        try:
                            inode = int(parts[9])
                        except (ValueError, IndexError):
                            pass
                        
                        # Create port info
                        port_info = PortInfo(
                            port=port,
                            protocol='tcp',
                            address=address
                        )
                        
                        # Try to find process by inode
                        if inode:
                            pid, process_name = self._find_process_by_inode(inode)
                            if pid:
                                port_info.pid = pid
                                port_info.process_name = process_name
                        
                        ports.append(port_info)
                        
            except (OSError, IOError) as e:
                logger.debug(f"Could not read {proc_file}: {e}")
                continue
        
        return ports
    
    def _find_process_by_inode(self, inode: int) -> Tuple[Optional[int], Optional[str]]:
        """Find process PID and name by socket inode"""
        try:
            import os
            import glob
            
            # Search through /proc/*/fd/* for the socket inode
            for pid_dir in glob.glob('/proc/[0-9]*'):
                try:
                    pid = int(os.path.basename(pid_dir))
                    fd_dir = os.path.join(pid_dir, 'fd')
                    
                    if not os.path.exists(fd_dir):
                        continue
                        
                    try:
                        for fd_link in os.listdir(fd_dir):
                            try:
                                fd_path = os.path.join(fd_dir, fd_link)
                                if os.path.islink(fd_path):
                                    target = os.readlink(fd_path)
                                    if target == f'socket:[{inode}]':
                                        # Found the process, get its name
                                        process_name = self._get_process_name(pid)
                                        return pid, process_name
                            except (OSError, ValueError, PermissionError):
                                continue
                    except (OSError, PermissionError):
                        # Can't read fd directory, try alternative approach
                        continue
                            
                except (ValueError, OSError):
                    continue
                    
        except Exception as e:
            logger.debug(f"Error finding process by inode {inode}: {e}")
            
        return None, None
    
    def _get_process_name(self, pid: int) -> Optional[str]:
        """Get process name for a given PID using multiple methods"""
        try:
            # Try /proc/pid/comm first (most reliable)
            try:
                with open(f'/proc/{pid}/comm', 'r') as f:
                    return f.read().strip()
            except (OSError, IOError):
                pass
            
            # Try /proc/pid/cmdline as fallback
            try:
                with open(f'/proc/{pid}/cmdline', 'r') as f:
                    cmdline = f.read().strip()
                    if cmdline:
                        # Get just the command name, not full path or arguments
                        cmd = cmdline.split('\x00')[0]  # cmdline is null-separated
                        if cmd:
                            return os.path.basename(cmd)
            except (OSError, IOError):
                pass
            
            # Try /proc/pid/stat as last resort
            try:
                with open(f'/proc/{pid}/stat', 'r') as f:
                    stat_line = f.read().strip()
                    # Process name is the second field in parentheses
                    start = stat_line.find('(')
                    end = stat_line.rfind(')')
                    if start != -1 and end != -1 and end > start:
                        return stat_line[start+1:end]
            except (OSError, IOError):
                pass
                
        except Exception as e:
            logger.debug(f"Error getting process name for PID {pid}: {e}")
            
        return None
    
    def find_available_port(self, preferred_port: int, address: str = '127.0.0.1', 
                          port_range: Tuple[int, int] = (1024, 65535)) -> Optional[int]:
        """
        Find an available port, starting with the preferred port
        
        Args:
            preferred_port: Preferred port number
            address: Address to bind to
            port_range: Range of ports to search (min, max)
            
        Returns:
            Available port number or None if none found
        """
        # First try the preferred port
        if self.is_port_available(preferred_port, address):
            return preferred_port
        
        # Search nearby ports first (±10 from preferred)
        search_range = 10
        start = max(port_range[0], preferred_port - search_range)
        end = min(port_range[1], preferred_port + search_range)
        
        for port in range(start, end + 1):
            if port != preferred_port and self.is_port_available(port, address):
                return port
        
        # If no nearby port found, search the full range
        for port in range(port_range[0], port_range[1] + 1):
            if port < start or port > end:  # Skip already checked range
                if self.is_port_available(port, address):
                    return port
        
        return None
    
    def get_port_conflicts(self, ports_to_check: List[int], 
                          address: str = '127.0.0.1') -> List[Tuple[int, PortInfo]]:
        """
        Check for conflicts with a list of ports
        
        Args:
            ports_to_check: List of port numbers to check
            address: Address to check against
            
        Returns:
            List of (port, PortInfo) tuples for conflicting ports
        """
        conflicts = []
        listening_ports = self.get_listening_ports()
        
        # Create lookup dict for faster searching
        port_lookup = {}
        for port_info in listening_ports:
            key = (port_info.port, port_info.address)
            port_lookup[key] = port_info
        
        for port in ports_to_check:
            # Check exact address match
            if (port, address) in port_lookup:
                conflicts.append((port, port_lookup[(port, address)]))
            # Check wildcard binding (0.0.0.0 conflicts with any address)
            elif (port, '0.0.0.0') in port_lookup:
                conflicts.append((port, port_lookup[(port, '0.0.0.0')]))
            # Check if we're binding to 0.0.0.0 and any specific address is using the port
            elif address == '0.0.0.0':
                for (check_port, check_addr), port_info in port_lookup.items():
                    if check_port == port:
                        conflicts.append((port, port_info))
                        break
        
        return conflicts

# Global port checker instance
_port_checker = None

def get_port_checker() -> PortChecker:
    """Get the global PortChecker instance"""
    global _port_checker
    if _port_checker is None:
        _port_checker = PortChecker()
    return _port_checker

# Convenience functions
def is_port_available(port: int, address: str = '127.0.0.1') -> bool:
    """Check if a port is available"""
    return get_port_checker().is_port_available(port, address)

def get_listening_ports() -> List[PortInfo]:
    """Get all listening ports"""
    return get_port_checker().get_listening_ports()

def find_available_port(preferred_port: int, address: str = '127.0.0.1') -> Optional[int]:
    """Find an available port near the preferred port"""
    return get_port_checker().find_available_port(preferred_port, address)

def check_port_conflicts(ports: List[int], address: str = '127.0.0.1') -> List[Tuple[int, PortInfo]]:
    """Check for port conflicts"""
    return get_port_checker().get_port_conflicts(ports, address)