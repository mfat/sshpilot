"""Backward-compatible re-export shim.

The pure SFTP v3 wire-protocol codec moved to :mod:`sshpilot.sftp.protocol` so
the daemon can import it without pulling in this (GObject-using) package.
Import from ``sshpilot.sftp.protocol`` in new code.
"""

from ..sftp.protocol import *  # noqa: F401,F403
from ..sftp.protocol import (  # noqa: F401
    PROTOCOL_VERSION,
    SFTPAttributes,
    SFTPError,
    _FX_NAMES,
    _Reader,
)
