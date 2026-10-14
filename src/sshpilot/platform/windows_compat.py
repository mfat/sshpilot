"""Windows compatibility layer for Unix-specific functionality.

This module provides Windows-compatible alternatives or graceful degradation
for Unix-specific features used in sshPilot.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# User/Group Information (pwd/grp modules)
# ============================================================================

class WindowsUserInfo:
    """Windows-compatible user information (mimics pwd.struct_passwd)."""
    
    def __init__(self):
        # Get username from environment or system
        self.pw_name = os.environ.get('USERNAME', os.environ.get('USER', 'user'))
        self.pw_uid = -1  # Windows doesn't have UIDs
        self.pw_gid = -1  # Windows doesn't have GIDs
        # Get user profile path (Windows equivalent of home directory)
        self.pw_dir = os.environ.get('USERPROFILE', os.path.expanduser('~'))
        # Windows uses COMSPEC for default shell, or PowerShell
        self.pw_shell = os.environ.get('COMSPEC', 'cmd.exe')
        # Try to use PowerShell if available
        if os.path.exists(r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'):
            self.pw_shell = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
        self.pw_gecos = ''  # Not applicable on Windows


def getpwuid_windows(uid: int) -> WindowsUserInfo:
    """Windows-compatible version of pwd.getpwuid().
    
    Args:
        uid: User ID (ignored on Windows)
        
    Returns:
        WindowsUserInfo object with current user information
    """
    return WindowsUserInfo()


def getpwnam_windows(name: str) -> WindowsUserInfo:
    """Windows-compatible version of pwd.getpwnam().
    
    Args:
        name: Username (currently returns current user info)
        
    Returns:
        WindowsUserInfo object
    """
    # For now, just return current user info
    # A full implementation would query Windows user database
    return WindowsUserInfo()


# ============================================================================
# Terminal I/O Control (termios module)
# ============================================================================

# Windows doesn't have termios, but we can provide stubs for graceful degradation
TCSADRAIN = 0  # Dummy value
TCSAFLUSH = 0  # Dummy value


def tcgetattr_stub(fd: int):
    """Stub for termios.tcgetattr() - not functional on Windows."""
    logger.debug("tcgetattr called on Windows (no-op)")
    return []


def tcsetattr_stub(fd: int, when: int, attributes):
    """Stub for termios.tcsetattr() - not functional on Windows."""
    logger.debug("tcsetattr called on Windows (no-op)")
    pass


def tcdrain_stub(fd: int):
    """Stub for termios.tcdrain() - not functional on Windows."""
    logger.debug("tcdrain called on Windows (no-op)")
    pass


# ============================================================================
# File Control (fcntl module)
# ============================================================================

# fcntl is already handled with try/except in platform/locking.py
# This is just for reference if needed elsewhere

LOCK_EX = 0x2  # Exclusive lock
LOCK_UN = 0x8  # Unlock
LOCK_NB = 0x4  # Non-blocking


def fcntl_stub(*args, **kwargs):
    """Stub for fcntl operations - not functional on Windows."""
    logger.debug("fcntl called on Windows (no-op)")
    pass


def flock_windows(fd: int, operation: int):
    """Windows file locking using msvcrt.
    
    This is a partial implementation that provides basic file locking
    functionality on Windows.
    """
    try:
        import msvcrt
        import os
        
        # Get file handle from file descriptor
        # Note: This is a simplified implementation
        if operation & LOCK_EX:
            # Try to lock the file
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK if operation & LOCK_NB else msvcrt.LK_LOCK, 1)
            except IOError:
                if operation & LOCK_NB:
                    raise BlockingIOError("Resource temporarily unavailable")
                raise
        elif operation & LOCK_UN:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except IOError:
                pass  # Ignore errors on unlock
    except ImportError:
        logger.warning("msvcrt not available for file locking")


# ============================================================================
# Platform Detection and Helpers
# ============================================================================

def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == 'win32' or os.name == 'nt'


def get_windows_ssh_dir() -> str:
    """Get the SSH directory on Windows.
    
    Returns the .ssh directory in the user's profile.
    """
    userprofile = os.environ.get('USERPROFILE', os.path.expanduser('~'))
    return os.path.join(userprofile, '.ssh')


def get_shell_for_windows() -> str:
    """Get the appropriate shell for Windows.
    
    Returns PowerShell if available, otherwise cmd.exe.
    """
    # Check for PowerShell Core (pwsh.exe) first
    pwsh_paths = [
        r'C:\Program Files\PowerShell\7\pwsh.exe',
        r'C:\Program Files (x86)\PowerShell\7\pwsh.exe',
    ]
    for pwsh in pwsh_paths:
        if os.path.exists(pwsh):
            return pwsh
    
    # Check for Windows PowerShell
    powershell_path = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    if os.path.exists(powershell_path):
        return powershell_path
    
    # Fall back to cmd.exe
    return os.environ.get('COMSPEC', 'cmd.exe')


def get_windows_user_info() -> Dict[str, str]:
    """Get current Windows user information.
    
    Returns:
        Dictionary with user information (name, home, shell)
    """
    return {
        'USER': os.environ.get('USERNAME', os.environ.get('USER', 'user')),
        'LOGNAME': os.environ.get('USERNAME', os.environ.get('USER', 'user')),
        'HOME': os.environ.get('USERPROFILE', os.path.expanduser('~')),
        'SHELL': get_shell_for_windows(),
    }


# ============================================================================
# Module-level compatibility exports
# ============================================================================

# Export platform-specific implementations
if is_windows():
    getpwuid = getpwuid_windows
    getpwnam = getpwnam_windows
    tcgetattr = tcgetattr_stub
    tcsetattr = tcsetattr_stub
    tcdrain = tcdrain_stub
    flock = flock_windows
else:
    # On Unix, use the real modules
    try:
        import pwd
        import grp
        import termios
        import fcntl
        
        getpwuid = pwd.getpwuid
        getpwnam = pwd.getpwnam
        tcgetattr = termios.tcgetattr
        tcsetattr = termios.tcsetattr
        tcdrain = termios.tcdrain
        flock = fcntl.flock
    except ImportError as e:
        logger.warning(f"Failed to import Unix modules: {e}")
        getpwuid = getpwuid_windows
        getpwnam = getpwnam_windows
        tcgetattr = tcgetattr_stub
        tcsetattr = tcsetattr_stub
        tcdrain = tcdrain_stub
        flock = lambda *args: None
