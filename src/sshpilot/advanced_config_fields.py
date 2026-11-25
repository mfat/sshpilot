"""Which option lines of a block the Structured view shows as typed fields
(GTK-free).

The Structured view shows a few keywords as fields of their own (HostName,
User, Port, the identity files, ProxyJump, the yes/no switches, the
forwards) and every other line in a table. Nothing is ever dropped or
rewritten to fit a field: a line a field cannot show as it is stays in the
table. So:

* a keyword with one value is shown by its field only for its **first**
  line in the block: OpenSSH keeps the first value it obtains, and a second
  line of the same keyword is a line of its own (not used), shown in the
  table where it can be seen and removed;
* a yes/no field takes its keyword's first line only when its value is
  ``yes`` or ``no``: an agent socket written after ``ForwardAgent`` stays
  in the table, as text, and the field shows nothing rather than a later
  line OpenSSH does not use;
* lines added from the table stay in the table while the block is on
  screen, even when their keyword is one a field shows: otherwise the row
  being typed into would jump to a field under the pointer (*keep*).

Also here: what a key file on disk says about itself, for the identity
file rows (missing, readable by others).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

#: Fields of one value, in the order the view shows them.
SINGLE = ("hostname", "user", "port", "proxyjump")
#: Fields of a yes/no choice ("not set" being the third).
SWITCHES = ("identitiesonly", "forwardagent")
IDENTITY = "identityfile"
FORWARDS = ("localforward", "remoteforward", "dynamicforward")

#: How each keyword is written when the view adds it.
SPELLING = {
    "hostname": "HostName",
    "user": "User",
    "port": "Port",
    "proxyjump": "ProxyJump",
    "identitiesonly": "IdentitiesOnly",
    "forwardagent": "ForwardAgent",
    "identityfile": "IdentityFile",
    "localforward": "LocalForward",
    "remoteforward": "RemoteForward",
    "dynamicforward": "DynamicForward",
}

_YES_NO = ("yes", "no")


@dataclass
class Claims:
    """The option uids each part of the Structured view shows."""

    single: Dict[str, int] = field(default_factory=dict)
    switches: Dict[str, int] = field(default_factory=dict)
    identities: List[int] = field(default_factory=list)
    forwards: List[int] = field(default_factory=list)
    other: List[int] = field(default_factory=list)


def _unquoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def claim(options: Iterable, keep: Iterable[int] = ()) -> Claims:
    """Share a block's *options* out between the fields and the table."""
    kept = set(keep)
    claims = Claims()
    #: Keywords whose first line has been seen: a field only ever shows the
    #: first, the one OpenSSH uses.
    seen: set = set()
    for option in options:
        keyword = option.keyword
        first = keyword not in seen
        seen.add(keyword)
        if option.uid in kept or not keyword:
            claims.other.append(option.uid)
        elif keyword in SINGLE and first:
            claims.single[keyword] = option.uid
        elif (
            keyword in SWITCHES and first
            and _unquoted(option.value).lower() in _YES_NO
        ):
            claims.switches[keyword] = option.uid
        elif keyword == IDENTITY:
            claims.identities.append(option.uid)
        elif keyword in FORWARDS:
            claims.forwards.append(option.uid)
        else:
            claims.other.append(option.uid)
    return claims


# -- key files ---------------------------------------------------------------

#: What a key file's status says (see :func:`key_file_status`).
KEY_OK = "ok"
KEY_MISSING = "missing"
KEY_OPEN = "open"               # readable or writable by others
KEY_UNCHECKED = "unchecked"     # tokens or variables: known only when connecting


@dataclass(frozen=True)
class KeyFileStatus:
    state: str
    path: str = ""
    #: The permission bits, for KEY_OPEN (e.g. 0o644).
    mode: int = 0


def key_path(value: str, home: Optional[str] = None) -> Optional[str]:
    """The file an IdentityFile value names, or None when it can only be
    known when connecting (``%d``, ``%h``, ``${VAR}``...)."""
    path = _unquoted(value)
    if not path or "%" in path or "${" in path:
        return None
    if path == "~" or path.startswith("~/"):
        path = (home or os.path.expanduser("~")) + path[1:]
    elif path.startswith("~"):
        return None                     # another user's home: not ours to judge
    return path


def key_file_status(value: str, home: Optional[str] = None) -> KeyFileStatus:
    path = key_path(value, home)
    if path is None:
        return KeyFileStatus(KEY_UNCHECKED)
    try:
        info = os.stat(path)
    except OSError:
        return KeyFileStatus(KEY_MISSING, path)
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return KeyFileStatus(KEY_OPEN, path, mode)
    return KeyFileStatus(KEY_OK, path, mode)


def fix_key_permissions(path: str) -> bool:
    """Make *path* readable and writable by its owner only, as OpenSSH
    requires of a private key. False when it could not be done."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def home_relative(path: str, home: Optional[str] = None) -> str:
    """*path* written as a configuration usually writes it: ``~/...`` under
    the home directory, and quoted when it holds a space."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    if path == home:
        path = "~"
    elif home and path.startswith(home + "/"):
        path = "~" + path[len(home):]
    return f'"{path}"' if any(char.isspace() for char in path) else path


def jump_hops(value: str) -> List[str]:
    """The hosts a ProxyJump value goes through, in order: ``user@host:port``
    written as it is. ``none`` and an empty value go through none."""
    value = _unquoted(value)
    if not value or value.lower() == "none":
        return []
    return [hop.strip() for hop in value.split(",") if hop.strip()]


def hop_host(hop: str) -> str:
    """The host a hop names, without its user and port, as OpenSSH is
    asked about it (``admin@bastion:2222`` -> ``bastion``)."""
    hop = hop.strip()
    hop = hop.split("://", 1)[-1]           # ssh://user@host:port
    hop = hop.rpartition("@")[2]
    if hop.startswith("["):
        return hop[1:].partition("]")[0]
    return hop.partition(":")[0]
