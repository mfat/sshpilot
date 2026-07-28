"""Stable persisted identity helpers for saved connections.

Connection UUIDs are stored as canonical primitive strings.  Public API IDs
remain opaque strings with a version-independent ``connection:`` namespace.
The legacy hash identifier is retained only as a bounded input compatibility
alias during Protocol v1.
"""

from __future__ import annotations

import hashlib
import shlex
import uuid as uuid_module
from typing import Any, Dict, Optional


CONNECTION_ID_PREFIX = "connection:"
TRANSITIONAL_CONNECTION_ID_PREFIX = "connection:v1:"
SSH_UUID_MARKER = "# sshpilot:ConnectionUUID"
MAX_CONNECTION_ID_LENGTH = 128


def canonical_connection_uuid(value: Any) -> str:
    """Return canonical UUID text or raise ``ValueError``.

    Nil UUIDs and non-string persistence values are invalid.  Persistence uses
    primitive lowercase hyphenated text rather than ``uuid.UUID`` objects.
    """

    if type(value) is not str:
        raise ValueError("connection UUID must be a string")
    text = value.strip()
    if not text or len(text) > 36:
        raise ValueError("connection UUID is invalid")
    try:
        parsed = uuid_module.UUID(text)
    except (AttributeError, ValueError):
        raise ValueError("connection UUID is invalid") from None
    if parsed.int == 0:
        raise ValueError("nil connection UUID is invalid")
    return str(parsed)


def new_connection_uuid() -> str:
    """Generate a canonical random UUIDv4 string."""

    return str(uuid_module.uuid4())


def connection_id_from_uuid(value: Any) -> str:
    """Return the canonical public ID for a persisted UUID."""

    return f"{CONNECTION_ID_PREFIX}{canonical_connection_uuid(value)}"


def connection_uuid_from_id(value: Any) -> str:
    """Parse a canonical UUID-backed public ID strictly."""

    if type(value) is not str:
        raise ValueError("connection ID must be a string")
    if not value or len(value) > MAX_CONNECTION_ID_LENGTH:
        raise ValueError("connection ID is invalid")
    if not value.startswith(CONNECTION_ID_PREFIX):
        raise ValueError("connection ID prefix is invalid")
    suffix = value[len(CONNECTION_ID_PREFIX):]
    if suffix.startswith("v1:"):
        raise ValueError("transitional connection ID is not UUID-backed")
    if len(suffix) != 36:
        raise ValueError("connection ID UUID is not in standard text form")
    canonical = canonical_connection_uuid(suffix)
    return canonical


def transitional_connection_id(protocol: Any, nickname: Any) -> str:
    """Reproduce the deprecated Phase 1-4 identifier for input aliases."""

    protocol_text = str(protocol or "ssh")
    nickname_text = str(nickname or "")
    digest = hashlib.sha256(
        f"{protocol_text}\0{nickname_text}".encode()
    ).hexdigest()
    return f"{TRANSITIONAL_CONNECTION_ID_PREFIX}{digest[:32]}"


def is_transitional_connection_id(value: Any) -> bool:
    """Return whether *value* is a strictly shaped legacy hash ID."""

    if type(value) is not str or len(value) != len(TRANSITIONAL_CONNECTION_ID_PREFIX) + 32:
        return False
    if not value.startswith(TRANSITIONAL_CONNECTION_ID_PREFIX):
        return False
    suffix = value[len(TRANSITIONAL_CONNECTION_ID_PREFIX):]
    return all(char in "0123456789abcdef" for char in suffix)


def parse_ssh_uuid_marker(line: str) -> Optional[tuple[Optional[str], str]]:
    """Parse a managed UUID comment.

    The current form is ``# sshpilot:ConnectionUUID <host-token> <uuid>``.
    A single UUID argument is accepted for early development fixtures and maps
    to the block's sole host token.
    """

    stripped = line.strip()
    if not stripped.startswith(SSH_UUID_MARKER):
        return None
    try:
        parts = shlex.split(stripped[len(SSH_UUID_MARKER):].strip())
    except ValueError:
        return (None, "")
    if len(parts) == 1:
        return (None, parts[0])
    if len(parts) == 2:
        return (parts[0], parts[1])
    return (None, "")


def uuid_markers_from_lines(
    lines: list[str],
    host_tokens: list[str],
) -> Dict[str, Any]:
    """Return raw marker values by host token for validation/repair."""

    values: Dict[str, Any] = {}
    sole_token = host_tokens[0] if len(host_tokens) == 1 else None
    for raw_line in lines:
        parsed = parse_ssh_uuid_marker(raw_line)
        if parsed is None:
            continue
        token, raw_uuid = parsed
        if token is None:
            if sole_token is not None:
                values[sole_token] = raw_uuid
            continue
        if token in host_tokens:
            values[token] = raw_uuid
    return values


def format_ssh_uuid_marker(host_token: str, connection_uuid: str) -> str:
    """Render one managed marker line without a trailing newline."""

    canonical = canonical_connection_uuid(connection_uuid)
    return f"    {SSH_UUID_MARKER} {shlex.quote(host_token)} {canonical}"
