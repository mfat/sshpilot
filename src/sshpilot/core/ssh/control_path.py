"""What OpenSSH accepts for a multiplexing master's socket (GTK-free).

Shared by the connection editor, which checks a Host block's own
``ControlPath`` and ``ControlPersist`` before saving them, and the daemon,
which checks the socket directory it injects for Settings ▸ SSH Options.
Both values fail late in OpenSSH: a bad ``ControlPersist`` refuses the whole
launch, and a socket the master cannot listen on makes ssh exit *after*
authenticating, so neither shows up until a connection is tried.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

#: ``sizeof(sockaddr_un.sun_path)``: 104 on macOS and the BSDs, 108 elsewhere.
SUN_PATH_SIZE = 104 if sys.platform == "darwin" or "bsd" in sys.platform else 108

#: The master first listens on ``<ControlPath>.<16 random characters>`` and
#: renames it into place, and the name must leave room for the NUL. A longer
#: path makes ssh exit after authenticating (``unix_listener: path ... too
#: long``), even though OpenSSH's own up-front check allows 17 bytes more.
MAX_CONTROL_PATH_LENGTH = SUN_PATH_SIZE - 1 - 17

#: ``%C`` expands to a SHA1 hex digest.
_HASH_LENGTH = 40

_TOKEN = re.compile(r"%(.)")

_PERSIST_TIME = re.compile(r"(?:\d+[smhdw]?)+", re.IGNORECASE)
_PERSIST_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_INT_MAX = 2**31 - 1


def _expand_known(path: str) -> Optional[str]:
    """*path* with ``~``, ``%d`` and ``%%`` expanded and ``%C`` replaced by
    a stand-in of its length; ``None`` when any other token is left, whose
    expansion depends on the connection."""
    home = os.path.expanduser("~")
    if path.startswith("~/") or path == "~":
        path = home + path[1:]
    unknown = False

    def replace(match: "re.Match[str]") -> str:
        nonlocal unknown
        token = match.group(1)
        if token == "%":
            return "%"
        if token == "d":
            return home
        if token == "C":
            return "0" * _HASH_LENGTH
        unknown = True
        return ""

    expanded = _TOKEN.sub(replace, path)
    return None if unknown else expanded


def control_path_directory(path: str) -> Optional[str]:
    """The directory a ``ControlPath`` puts its socket in, expanded; ``None``
    for ``none``, an empty value, or a directory naming a per-connection
    token (it cannot be known before the connection)."""
    path = (path or "").strip()
    if not path or path.lower() == "none":
        return None
    directory = os.path.dirname(path)
    if not directory:
        return None
    return _expand_known(directory)


def control_path_too_long(path: str) -> bool:
    """Whether the master could not listen on *path*: True only when its
    expanded length is known and over :data:`MAX_CONTROL_PATH_LENGTH`."""
    path = (path or "").strip()
    if not path or path.lower() == "none":
        return False
    expanded = _expand_known(path)
    return expanded is not None and len(expanded.encode()) > MAX_CONTROL_PATH_LENGTH


def control_persist_valid(value: str) -> bool:
    """Whether OpenSSH accepts *value* for ``ControlPersist``: ``yes``,
    ``true``, ``no``, ``false`` (lower case only, as ssh compares them), or a
    time such as ``60``, ``10m`` or ``1h30m``."""
    value = (value or "").strip()
    if value in ("yes", "true", "no", "false"):
        return True
    if not _PERSIST_TIME.fullmatch(value):
        return False
    total = 0
    for number, unit in re.findall(r"(\d+)([smhdw]?)", value, re.IGNORECASE):
        total += int(number) * _PERSIST_UNITS[unit.lower()]
        if total > _INT_MAX:
            return False
    return True
