"""Helpers for the Directives view (GTK-free): how the common options are
spelled when added, what a key file on disk says about itself, the hops of
a ProxyJump, and what an answer of ``ssh -G`` comes to.

Every line of a block is shown the same way, a keyword and a value (the
user chose plain rows over typed fields for a few keywords).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

#: Common options that are yes or no: added with "yes".
SWITCHES = ("identitiesonly", "forwardagent")

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

def _unquoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


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
    return os.path.normpath(path) if os.name == "nt" else path


def key_file_status(value: str, home: Optional[str] = None) -> KeyFileStatus:
    path = key_path(value, home)
    if path is None:
        return KeyFileStatus(KEY_UNCHECKED)
    try:
        info = os.stat(path)
    except OSError:
        return KeyFileStatus(KEY_MISSING, path)
    if os.name == "nt":
        # Windows uses ACLs. stat() cannot establish who may read the key.
        return KeyFileStatus(KEY_UNCHECKED, path)
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return KeyFileStatus(KEY_OPEN, path, mode)
    return KeyFileStatus(KEY_OK, path, mode)


#: Options of ``ssh -G`` whose values name files OpenSSH must find, and
#: whether the file is a private key (one other users must not read).
_VALUE_FILES = {"identityfile": True, "certificatefile": False}


def value_file_problems(key: str, values: List[str], *, default: bool,
                        home: Optional[str] = None) -> List[KeyFileStatus]:
    """The files an option's values (as ``ssh -G`` prints them) name that
    are missing, or, for a private key, readable by other users. OpenSSH's
    own defaults (``~/.ssh/id_rsa`` and the rest, tried only when there)
    are no problem; nor is what is known only when connecting."""
    private = _VALUE_FILES.get(key)
    if private is None or default:
        return []
    found = []
    for value in values:
        path = _unquoted(value)
        if not (os.path.isabs(path) or path.startswith(("/", "~/"))):
            continue                        # "none", tokens, another user's home
        status = key_file_status(value, home)
        if status.state == KEY_MISSING or (private and status.state == KEY_OPEN):
            found.append(status)
    return found


def fix_key_permissions(path: str) -> bool:
    """Make *path* readable and writable by its owner only, as OpenSSH
    requires of a private key. False when it could not be done."""
    if os.name == "nt":
        from .platform.windows.acl import restrict_to_current_user
        return restrict_to_current_user(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


#: Keywords whose value is a key or certificate file (Browse, its status);
#: True for a private key, which others must not be able to read.
KEY_FILE_KEYWORDS = {"identityfile": True, "certificatefile": False}


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
    if "://" in hop:                        # ssh://user@host:port/
        hop = hop.split("://", 1)[1].rstrip("/")
    hop = hop.rpartition("@")[2]
    if hop.startswith("["):
        return hop[1:].partition("]")[0]
    return hop.partition(":")[0]


@dataclass
class ConnectionSummary:
    """What an answer of ``ssh -G`` comes to, for the header of Effective."""

    hostname: str
    user: str
    port: str
    #: The port is OpenSSH's own (22): nothing set it.
    port_default: bool
    #: Hosts a ProxyJump goes through, in order (empty: none).
    hops: List[str]
    #: A ProxyCommand in use ("" when none).
    proxy_command: str
    #: Key files set by the configuration (empty: OpenSSH's default keys).
    keys: List[str]
    identities_only: bool


def connection_summary(values: Dict[str, List[str]], defaults: Iterable[str],
                       home: Optional[str] = None) -> ConnectionSummary:
    """Sum up the options of ``ssh -G`` (*values*: key -> values as printed;
    *defaults*: keys left at OpenSSH's default)."""
    defaults = set(defaults)

    def one(key: str) -> str:
        found = values.get(key) or [""]
        return found[0].strip()

    hostname, user, port = one("hostname"), one("user"), one("port") or "22"
    jump = one("proxyjump")
    hops = jump_hops(jump) if jump.lower() != "none" else []
    command = one("proxycommand")
    proxy_command = "" if command.lower() in ("", "none") else command
    keys = [] if "identityfile" in defaults else [
        home_relative(key_path(value, home) or _unquoted(value), home)
        for value in values.get("identityfile", ())
    ]
    return ConnectionSummary(
        hostname=hostname, user=user, port=port, port_default="port" in defaults,
        hops=hops, proxy_command=proxy_command, keys=keys,
        identities_only=one("identitiesonly").lower() == "yes",
    )


def block_detail(block) -> str:
    """``user@hostname`` as the block itself sets them; empty when it sets
    neither."""
    values: Dict[str, str] = {}
    for option in block.options:
        keyword = option.keyword
        if keyword in ("user", "hostname") and keyword not in values:
            values[keyword] = option.value.strip().strip('"')
    user, host = values.get("user", ""), values.get("hostname", "")
    if user and host:
        return f"{user}@{host}"
    # Without an address of its own, what the block is says more than a
    # user name alone.
    return host
