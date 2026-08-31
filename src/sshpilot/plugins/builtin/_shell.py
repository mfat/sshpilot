"""Shared helper for built-in protocol backends: shell-word fields.

Several backends let the user type a shell fragment (docker/k8s ``command``,
mosh ``extra_ssh_opts``) and split it with :func:`shlex.split`.  A stray quote
makes that raise :class:`ValueError`, which is a *user input* problem but
would surface as an unexpected internal failure: ``validate`` would pass it,
so the dialog saves happily, and the daemon's protocol launch only converts
:class:`ProtocolError` into a reportable error.

These two helpers keep such a typo on the validation path in both directions —
reported in the editor before saving, and reported as a ``ProtocolError`` if a
connection reaches a spawn some other way (the plugin API, an imported
backup).
"""

from __future__ import annotations

import shlex
from typing import List, Optional

from ...api.models.sessions import PluginSessionFailureCode
from ._session_failure import BuiltinProtocolError

__all__ = ["command_split_diagnostic", "split_command"]

_LEGACY_FIELD_LABELS = {
    "command": "Command",
    "extra_ssh_opts": "Extra SSH options",
}


def _split(value: str) -> List[str]:
    return shlex.split(str(value or ""))


def command_split_diagnostic(value: object) -> Optional[str]:
    """Return an opaque parser diagnostic for frontend validation.

    The frontend caller owns the localizable template. Keeping only the raw
    diagnostic here prevents the daemon-facing ``split_command`` path from
    acquiring gettext behaviour.
    """
    if not value:
        return None
    try:
        _split(value)
    except ValueError as exc:
        return str(exc)
    return None


def split_command(value: object, field: str) -> List[str]:
    """Split a stored command and keep its parser diagnostic out of the code."""

    if field not in _LEGACY_FIELD_LABELS:
        raise ValueError("unknown built-in plugin command field")
    try:
        return _split(value)
    except ValueError as exc:
        diagnostic = str(exc)
        raise BuiltinProtocolError(
            PluginSessionFailureCode.ARGUMENTS_INVALID,
            f"{_LEGACY_FIELD_LABELS[field]} could not be parsed: {diagnostic}.",
            parameters={"field": field},
            diagnostic=diagnostic,
        ) from exc
