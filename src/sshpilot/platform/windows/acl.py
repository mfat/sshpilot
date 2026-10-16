"""Owner-only file ACLs on Windows.

``os.chmod(path, 0o600)`` only toggles the read-only bit on Windows. OpenSSH
for Windows instead checks the file's ACL and refuses a private key that any
principal other than the owner, SYSTEM or Administrators can read
("UNPROTECTED PRIVATE KEY FILE"). The daemon endpoint file carries an auth
token and gets the same treatment.
"""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000


def _current_principal() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def restrict_to_current_user(path: os.PathLike) -> bool:
    """Replace ``path``'s ACL with full control for the current user only.

    SYSTEM keeps access (as OpenSSH's own ``ssh-keygen`` leaves it). Returns
    False, after logging, when the ACL could not be applied; callers treat
    this as best-effort since the file already lives in the user profile.
    """

    if sys.platform != "win32":
        return True
    command = [
        "icacls",
        os.fspath(path),
        "/inheritance:r",
        "/grant:r",
        f"{_current_principal()}:F",
        "/grant:r",
        "*S-1-5-18:F",  # NT AUTHORITY\SYSTEM, locale-independent
        "/remove:g",
        "*S-1-3-4",  # OWNER RIGHTS: Python 3.13+ mkdtemp/mkdir(0o700) grant it, OpenSSH refuses it
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not restrict file ACL: %s", type(exc).__name__)
        return False
    if result.returncode != 0:
        logger.warning(
            "icacls failed for %s (exit %s): %s",
            os.fspath(path),
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )
        return False
    return True


def restrict_directory_to_current_user(path: os.PathLike) -> bool:
    """Make ``path`` (a directory) private, for everything created inside it.

    The ACL grants the current user and SYSTEM full control, inherited by
    files and subdirectories (``(OI)(CI)``), with no inherited entries from
    the parent. OpenSSH for Windows refuses a config file other principals
    can reach ("Bad owner or permissions"); a file in ``%TEMP%`` otherwise
    inherits ``OWNER RIGHTS`` and is refused. Returns False after logging if
    icacls fails.
    """

    if sys.platform != "win32":
        return True
    command = [
        "icacls",
        os.fspath(path),
        "/inheritance:r",
        "/grant:r",
        f"{_current_principal()}:(OI)(CI)F",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",  # NT AUTHORITY\SYSTEM, locale-independent
        "/remove:g",
        "*S-1-3-4",  # OWNER RIGHTS: Python 3.13+ mkdtemp/mkdir(0o700) grant it, OpenSSH refuses it
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not restrict directory ACL: %s", type(exc).__name__)
        return False
    if result.returncode != 0:
        logger.warning(
            "icacls failed for %s (exit %s): %s",
            os.fspath(path),
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return False
    return True


def drop_owner_rights(path: os.PathLike) -> bool:
    """Remove the OWNER RIGHTS entry Python gives a private directory.

    On Windows, Python 3.13+ creates ``os.makedirs(..., mode=0o700)`` and
    ``tempfile.mkdtemp()`` directories with a protected ACL of SYSTEM,
    Administrators and OWNER RIGHTS. Everything inside inherits OWNER
    RIGHTS, and OpenSSH for Windows then refuses any config file there
    ("Bad owner or permissions", exit 255). Removing the entry, and granting
    the current user explicitly, propagates to the existing children too.
    """

    if sys.platform != "win32" or not os.path.isdir(path):
        return True
    command = [
        "icacls",
        os.fspath(path),
        "/remove:g",
        "*S-1-3-4",  # OWNER RIGHTS
        "/grant",
        f"{_current_principal()}:(OI)(CI)F",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not adjust directory ACL: %s", type(exc).__name__)
        return False
    if result.returncode != 0:
        logger.warning(
            "icacls failed for %s (exit %s): %s",
            os.fspath(path),
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return False
    return True


def chmod_private(path: os.PathLike, mode: int = 0o600) -> None:
    """``os.chmod`` on POSIX; owner-only ACL on Windows."""

    if sys.platform == "win32":
        restrict_to_current_user(path)
    else:
        os.chmod(path, mode)


_OWNER_RIGHTS_SID = "S-1-3-4"


def has_owner_rights_entry(path: os.PathLike) -> "bool | None":
    """Whether ``path``'s DACL has an allow entry for OWNER RIGHTS.

    Read through the Win32 security API, so no ``icacls`` process starts.
    ``None`` when the ACL could not be read (or off Windows).
    """

    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    class _AclHeader(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte),
            ("AclSize", ctypes.c_ushort), ("AceCount", ctypes.c_ushort),
            ("Sbz2", ctypes.c_ushort),
        ]

    se_file_object = 1
    dacl_security_information = 0x4
    access_allowed_ace_type = 0
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    owner_rights = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        os.fspath(path), se_file_object, dacl_security_information,
        None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if status != 0:
        return None
    try:
        if not dacl.value:
            return False  # a NULL DACL grants everyone; nothing to remove
        if not advapi32.ConvertStringSidToSidW(_OWNER_RIGHTS_SID, ctypes.byref(owner_rights)):
            return None
        header = _AclHeader.from_address(dacl.value)
        for index in range(header.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return None
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            if ace_type != access_allowed_ace_type:
                continue
            # ACCESS_ALLOWED_ACE: 4-byte header, 4-byte mask, then the SID.
            if advapi32.EqualSid(ace.value + 8, owner_rights):
                return True
        return False
    finally:
        if owner_rights.value:
            kernel32.LocalFree(owner_rights)
        kernel32.LocalFree(descriptor)


def repair_owner_rights(path: os.PathLike) -> bool:
    """Drop OWNER RIGHTS from ``path`` when it is there.

    For directories an earlier run (or another Python tool) created with
    ``mode=0o700``: OpenSSH for Windows refuses keys and configs inside them.
    Starts ``icacls`` only when the entry is present, or when the ACL could
    not be read. A missing directory or a non-Windows host is left alone.
    """

    if sys.platform != "win32" or not os.path.isdir(path):
        return True
    if has_owner_rights_entry(path) is False:
        return True
    return drop_owner_rights(path)
