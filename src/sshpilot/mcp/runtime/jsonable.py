"""Safe JSON conversion for runtime MCP tool output.

Mirrors the dataclass-walking shape of ``sshpilot.cli.output.to_jsonable`` but
lives in the MCP package so the runtime server needs no frontend dependency.
Unlike the CLI, the MCP boundary treats ``field(repr=False)`` as a daemon
declared "not for unstructured output" marker and redacts those values before
they reach model context; content-bearing reads require an explicit opt-in
(see :class:`RuntimePolicy`).

Certain fields are always redacted even when content is opted in: passwords,
passphrases, private key material, plugin secrets, and interaction claim
nonces must never enter model context.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, FrozenSet, Mapping

#: Placeholder used where a daemon-declared sensitive DTO field was withheld.
REDACTED = "<redacted>"

#: Field names that stay redacted even with ``SSHPILOT_MCP_CONTENT`` / daemon
#: content opt-in. Keep this list narrow and name-based so composed tools can
#: still return command stdout/stderr when content is allowed.
ALWAYS_REDACT_FIELDS: FrozenSet[str] = frozenset(
    {
        "nonce",
        "password",
        "passphrase",
        "secret",
        "private_key",
        "private_key_path",
        "plugin_secret",
        "master_password",
        "session_key",
        "token",
        "api_key",
        "client_secret",
    }
)


def to_jsonable(
    value: Any,
    *,
    redact_sensitive: bool = True,
    always_redact_fields: FrozenSet[str] | None = None,
) -> Any:
    """Convert public API values to stable JSON-compatible values.

    When ``redact_sensitive`` is true (the default), dataclass fields that the
    API declared with ``field(repr=False)`` are emitted as :data:`REDACTED`
    instead of their real value. Fields named in
    :data:`ALWAYS_REDACT_FIELDS` (or *always_redact_fields*) are redacted
    regardless of the content opt-in.
    """
    denylist = ALWAYS_REDACT_FIELDS if always_redact_fields is None else always_redact_fields
    return _convert(value, redact_sensitive=redact_sensitive, denylist=denylist)


def _convert(value: Any, *, redact_sensitive: bool, denylist: FrozenSet[str]) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        result = {}
        for item in fields(value):
            if item.name in denylist or (redact_sensitive and item.repr is False):
                result[item.name] = REDACTED
            else:
                result[item.name] = _convert(
                    getattr(value, item.name),
                    redact_sensitive=redact_sensitive,
                    denylist=denylist,
                )
        return result
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if str(key) in denylist
                else _convert(item, redact_sensitive=redact_sensitive, denylist=denylist)
            )
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _convert(item, redact_sensitive=redact_sensitive, denylist=denylist)
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        converted = [
            _convert(item, redact_sensitive=redact_sensitive, denylist=denylist)
            for item in value
        ]
        return sorted(converted, key=lambda item: str(item))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value
