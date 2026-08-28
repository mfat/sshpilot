"""Small SSH helpers shared across the app.

Command construction lives in ``ssh_connection_builder`` /
``core.ssh.build_ssh_process_spec``; this module only holds stderr
classification and the Flatpak HOME shim.
"""

import os
import logging
from typing import Dict

from .platform_utils import is_flatpak

logger = logging.getLogger(__name__)

# Markers shared by FM / SCP when classifying a failed ssh/scp run as auth.
# "permission denied" alone is NOT a marker: scp prints it for remote *file*
# permission errors ("scp: /path: Permission denied"). SSH auth failures use
# the parenthesized method list or the retry phrasing.
_SSH_AUTH_FAILURE_MARKERS = (
    'permission denied (',
    'permission denied, please try again',
    'authentication failed',
    'too many authentication failures',
)


def is_ssh_auth_failure_text(text: str) -> bool:
    """True when *text* looks like an SSH authentication failure."""
    lowered = (text or '').lower()
    return any(marker in lowered for marker in _SSH_AUTH_FAILURE_MARKERS)


def clean_ssh_stderr(text: str) -> str:
    """Drop ``ssh -v`` ``debug`` chatter, leaving the human-meaningful lines.

    With verbose logging on, ssh floods stderr with ``debugN:`` lines; showing
    that raw log in the UI is noise. Returns the stripped, joined remainder.
    """
    return "\n".join(
        line.strip()
        for line in (text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("debug")
    ).strip()


def ensure_writable_ssh_home(env: Dict[str, str]) -> None:
    """Ensure ssh-copy-id has a writable HOME when running in Flatpak."""
    if is_flatpak():
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        alt_home = os.path.join(runtime_dir, "sshcopyid-home")
        os.makedirs(os.path.join(alt_home, ".ssh"), exist_ok=True)
        env["HOME"] = alt_home
        logger.debug(f"Using temporary HOME for ssh-copy-id: {alt_home}")
