"""Minimal non-GTK askpass client for the daemon interaction broker."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys

_MAX_RESPONSE = 16 * 1024
_MAX_REQUEST = 8 * 1024
_HELPER_TIMEOUT_SECONDS = 605
_LENGTH = struct.Struct(">I")


def _receive_exact(transport: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = transport.recv(size - len(chunks))
        if not chunk:
            raise EOFError
        chunks.extend(chunk)
    return bytes(chunks)


def main() -> int:
    """Return only a brokered secret on stdout, never diagnostics."""

    socket_path = os.environ.get("SSHPILOT_DAEMON_ASKPASS_SOCKET", "")
    token = os.environ.get("SSHPILOT_DAEMON_ASKPASS_TOKEN", "")
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if (
        not socket_path
        or not token
        or len(socket_path) > 4096
        or len(token) > 256
        or len(prompt) > 4096
    ):
        return 1
    request = json.dumps(
        {
            "token": token,
            "prompt": prompt,
            "hint": os.environ.get("SSH_ASKPASS_PROMPT", ""),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > _MAX_REQUEST:
        return 1
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as transport:
            # Presence notifications may legitimately remain open while a user
            # locates and touches a hardware key.
            transport.settimeout(_HELPER_TIMEOUT_SECONDS)
            transport.connect(socket_path)
            transport.sendall(_LENGTH.pack(len(request)) + request)
            size = _LENGTH.unpack(_receive_exact(transport, _LENGTH.size))[0]
            if size > _MAX_RESPONSE:
                return 1
            secret = bytearray(_receive_exact(transport, size))
        sys.stdout.buffer.write(secret)
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
        secret[:] = b"\0" * len(secret)
        secret.clear()
        return 0
    except (EOFError, OSError, ValueError, json.JSONDecodeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
