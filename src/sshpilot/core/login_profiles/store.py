"""Hardened persistence for ``login_profiles.json`` (GTK-free).

Reuses the connection-state file primitives: symlinks are refused, reads are
size-limited strict UTF-8 JSON objects, and writes go through a same-directory
temporary file with ``fsync`` and an atomic replace. The file holds no secrets.
"""

from __future__ import annotations

from pathlib import Path

from ..connections.state_file import (
    _atomic_write_bytes,
    _decode_json_object,
    _read_state_bytes,
    _serialize_json,
)
from ..errors import CoreError, ErrorCode
from .models import LoginProfileState

_MAX_BYTES = 4 * 1024 * 1024


def read_login_profiles(path: Path) -> LoginProfileState:
    """Read the profile file; a missing file is an empty state."""
    raw = _read_state_bytes(Path(path), max_bytes=_MAX_BYTES)
    if raw is None:
        return LoginProfileState()
    data = _decode_json_object(raw)
    try:
        return LoginProfileState.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Login profile file is malformed",
            diagnostic_category="invalid_login_profiles",
            diagnostic_reason="login profile file is invalid",
        ) from exc


def write_login_profiles(path: Path, state: LoginProfileState) -> None:
    if type(state) is not LoginProfileState:
        raise TypeError("state must be a LoginProfileState")
    content = _serialize_json(state.to_dict(), "Login-profile")
    _atomic_write_bytes(
        Path(path), content, prefix=".login-profiles-", max_bytes=_MAX_BYTES
    )
