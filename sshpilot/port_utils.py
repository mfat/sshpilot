"""
Port utilities for sshPilot
Provides port information and availability checking functionality
"""

import socket
import subprocess
import logging
from typing import List, Dict, Optional, Tuple, Any
import json
import re

# Try to import psutil, fallback to subprocess methods if not available
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

logger = logging.getLogger(__name__)

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