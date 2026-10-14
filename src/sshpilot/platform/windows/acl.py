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
