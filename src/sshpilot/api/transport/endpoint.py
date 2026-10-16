"""Daemon endpoint transport: Unix-domain sockets, or loopback TCP on Windows.

POSIX keeps the established model: the endpoint *is* a mode-0600 Unix socket
inside a mode-0700 per-user directory, and filesystem permissions plus peer
credentials are the authority.

CPython on Windows has no ``AF_UNIX``, so there the daemon listens on an
ephemeral ``127.0.0.1`` port and publishes ``{"port", "token", "pid"}`` in a
small JSON file at the path the rest of the code calls the "socket path".
That file lives under ``%LOCALAPPDATA%`` (per-user) and is additionally
restricted to the current user. Every connection must open with
:func:`auth_preamble` for the published token before a single framed byte is
dispatched; anything else is dropped. The same mechanism serves the askpass
listener.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

#: True where endpoints are loopback TCP plus a published token.
USES_TCP_ENDPOINT = sys.platform == "win32" or not hasattr(socket, "AF_UNIX")

_PREAMBLE_PREFIX = b"SSHPILOT-AUTH "
# token_urlsafe(32) is always 43 ASCII characters.
_TOKEN_LENGTH = 43
#: Exact byte length of the authentication preamble a TCP client sends first.
PREAMBLE_LENGTH = len(_PREAMBLE_PREFIX) + _TOKEN_LENGTH + 1


class EndpointUnavailableError(ConnectionRefusedError):
    """No endpoint is published, or the published one refuses connections."""


@dataclass(frozen=True)
class TcpEndpoint:
    port: int
    token: str
    pid: Optional[int] = None


def new_token() -> str:
    token = secrets.token_urlsafe(32)
    assert len(token) == _TOKEN_LENGTH
    return token


def auth_preamble(token: str) -> bytes:
    return _PREAMBLE_PREFIX + token.encode("ascii") + b"\n"


def preamble_matches(preamble: bytes, token: str) -> bool:
    """Constant-time check of a received preamble against ``token``."""

    return hmac.compare_digest(bytes(preamble), auth_preamble(token))


def read_tcp_endpoint(path: os.PathLike) -> Optional[TcpEndpoint]:
    """Parse a published endpoint file; ``None`` when absent or malformed."""

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        port = int(data["port"])
        token = str(data["token"])
        pid = data.get("pid")
        pid = int(pid) if pid is not None else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (0 < port < 65536) or len(token) != _TOKEN_LENGTH:
        return None
    return TcpEndpoint(port=port, token=token, pid=pid)


def publish_tcp_endpoint(
    path: os.PathLike, endpoint: TcpEndpoint
) -> Tuple[int, int]:
    """Atomically write the endpoint file; return its ``(st_dev, st_ino)``."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"port": endpoint.port, "token": endpoint.token, "pid": endpoint.pid}
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        restrict_to_current_user(tmp_name)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    info = target.stat()
    return info.st_dev, info.st_ino


def restrict_to_current_user(path: os.PathLike) -> None:
    """Best-effort: limit ``path`` to the current user (no-op on POSIX)."""

    if sys.platform != "win32":
        return
    from sshpilot.platform.windows.acl import restrict_to_current_user as _restrict

    _restrict(path)


def restrict_directory_to_current_user(path: os.PathLike) -> None:
    """Best-effort: limit ``path`` and what is created in it (no-op on POSIX).

    Unlike :func:`restrict_to_current_user`, the user's grant is inherited, so
    files written into the directory stay readable and runnable by the user.
    """

    if sys.platform != "win32":
        return
    from sshpilot.platform.windows.acl import (
        restrict_directory_to_current_user as _restrict,
    )

    _restrict(path)


def create_tcp_listener() -> Tuple[socket.socket, int]:
    """Bind an ephemeral loopback listener; return it and its port."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows: forbid another process from binding over our port.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    return listener, int(listener.getsockname()[1])


def connect_tcp(
    endpoint: TcpEndpoint, *, timeout: Optional[float] = None
) -> socket.socket:
    """Connect to a TCP endpoint and send its authentication preamble."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        sock.connect(("127.0.0.1", endpoint.port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(auth_preamble(endpoint.token))
    except BaseException:
        sock.close()
        raise
    return sock


def connect(path: os.PathLike, *, timeout: Optional[float] = None) -> socket.socket:
    """Open an authenticated stream to the daemon endpoint at ``path``.

    Raises :class:`FileNotFoundError` / :class:`ConnectionRefusedError` (or
    another :class:`OSError`) exactly like a Unix-socket ``connect`` would, so
    callers keep a single error contract on both transports.
    """

    if not USES_TCP_ENDPOINT:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect(str(path))
        except BaseException:
            sock.close()
            raise
        return sock
    if not os.path.exists(path):
        raise FileNotFoundError(str(path))
    endpoint = read_tcp_endpoint(path)
    if endpoint is None:
        raise EndpointUnavailableError("The daemon endpoint file is unreadable")
    return connect_tcp(endpoint, timeout=timeout)


class PreambleGate:
    """Incrementally validate a TCP client's preamble on a non-blocking socket.

    ``feed`` returns ``None`` while more bytes are needed, ``False`` on a bad
    token, or the bytes that followed a valid preamble (possibly empty).
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._buffer = bytearray()

    def feed(self, chunk: bytes):
        self._buffer.extend(chunk)
        if len(self._buffer) < PREAMBLE_LENGTH:
            expected = auth_preamble(self._token)[: len(self._buffer)]
            if not hmac.compare_digest(bytes(self._buffer), expected):
                return False
            return None
        head = bytes(self._buffer[:PREAMBLE_LENGTH])
        rest = bytes(self._buffer[PREAMBLE_LENGTH:])
        self._buffer.clear()
        if not preamble_matches(head, self._token):
            return False
        return rest


def read_preamble_blocking(sock: socket.socket, token: str, *, timeout: float = 5.0) -> bool:
    """Read and verify the preamble on a blocking socket (askpass listener)."""

    previous = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        data = b""
        while len(data) < PREAMBLE_LENGTH:
            chunk = sock.recv(PREAMBLE_LENGTH - len(data))
            if not chunk:
                return False
            data += chunk
        return preamble_matches(data, token)
    except OSError:
        return False
    finally:
        sock.settimeout(previous)
