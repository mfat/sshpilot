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


def chmod_private(path: os.PathLike, mode: int = 0o600) -> None:
    """``os.chmod`` on POSIX; owner-only ACL on Windows."""

    if sys.platform == "win32":
        restrict_to_current_user(path)
    else:
        os.chmod(path, mode)
