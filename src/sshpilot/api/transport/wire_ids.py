"""Deterministic 32-byte wire encoding for runtime resource IDs.

Terminal and secret binary frames reserve a fixed 32-byte field for
session/attachment/interaction ids. Those ids used to be UUIDs whose
``.bytes`` round-tripped across processes. Counter tokens
(``session-1``, ``attachment-3``, …) are short UTF-8 strings; encode them
as null-padded UTF-8 so daemon and UI decode the same value without a
process-local lookup table.
"""

from __future__ import annotations

_WIRE_ID_SIZE = 32


def id_to_wire_bytes(id_str: str) -> bytes:
    """Encode a runtime id into a fixed 32-byte wire field."""
    if not isinstance(id_str, str) or not id_str:
        raise ValueError("wire id must be a non-empty string")
    if "\0" in id_str:
        raise ValueError("wire id must not contain NUL")
    raw = id_str.encode("utf-8")
    if len(raw) > _WIRE_ID_SIZE:
        raise ValueError(
            f"wire id exceeds {_WIRE_ID_SIZE} UTF-8 bytes: {id_str!r}"
        )
    return raw.ljust(_WIRE_ID_SIZE, b"\0")


def wire_bytes_to_id(raw: bytes) -> str:
    """Decode a fixed 32-byte wire field back to the runtime id string."""
    if not isinstance(raw, bytes) or len(raw) != _WIRE_ID_SIZE:
        raise ValueError("wire id field must be exactly 32 bytes")
    try:
        return raw.rstrip(b"\0").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("wire id field is not valid UTF-8") from exc
