"""Small validation helpers for frontend-safe public values."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional, Set


_SENSITIVE_DETAIL_KEY_PARTS = frozenset(
    {
        "argv",
        "authorization",
        "command",
        "cookie",
        "credential",
        "env",
        "environment",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


# -- Structural validation (transport-safe, no key blacklist) ----------------

def _check_structural_key(key: Any, path: str) -> str:
    """Validate a dict key is a non-empty string; no content filter."""
    if type(key) is not str or not key:
        raise TypeError(f"{path} keys must be non-empty strings")
    return key


def _copy_structural_value(value: Any, path: str, active: Set[int]) -> Any:
    """Validate and detach a JSON-safe value.  No key-name restrictions."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic list")
        active.add(identity)
        try:
            return [
                _copy_structural_value(item, f"{path}[]", active)
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic dictionary")
        active.add(identity)
        try:
            return {
                _check_structural_key(key, path): _copy_structural_value(
                    item,
                    f"{path}.{key}",
                    active,
                )
                for key, item in value.items()
            }
        finally:
            active.remove(identity)
    raise TypeError(
        f"{path} contains unsupported public value type "
        f"{type(value).__name__}"
    )


def copy_transport_value(value: Any, field_name: str = "value") -> Any:
    """Validate and detach a value for JSON transport (requests, responses,
    event payloads).  Structural checks only — no key-name restrictions,
    because SSH config field names like ``remote_command`` are legitimate."""
    try:
        return _copy_structural_value(value, field_name, set())
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"{field_name} is not a safe transport value") from None


# -- Sensitive-key filtering (error details only) ---------------------------

def _check_sensitive_key(key: Any, path: str) -> str:
    """Validate a key and reject names that conventionally carry secrets."""
    if type(key) is not str or not key:
        raise TypeError(f"{path} keys must be non-empty strings")
    normalized = key.lower().replace("-", "_")
    if any(part in normalized for part in _SENSITIVE_DETAIL_KEY_PARTS):
        raise ValueError(f"{path} contains a disallowed sensitive detail key")
    return key


def _copy_safe_detail_value(value: Any, path: str, active: Set[int]) -> Any:
    """Validate and detach a structured public error/event details value.

    Combines structural validation with sensitive-key protection.
    """
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic list")
        active.add(identity)
        try:
            return [
                _copy_safe_detail_value(item, f"{path}[]", active)
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic dictionary")
        active.add(identity)
        try:
            return {
                _check_sensitive_key(key, path): _copy_safe_detail_value(
                    item,
                    f"{path}.{key}",
                    active,
                )
                for key, item in value.items()
            }
        finally:
            active.remove(identity)
    raise TypeError(
        f"{path} contains unsupported public value type "
        f"{type(value).__name__}"
    )


def copy_safe_details(
    details: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate and detach a structured public error/event details mapping.

    Only JSON-safe scalar, list, and dictionary values are accepted. Keys that
    conventionally carry credentials, environments, or process command lines
    are rejected so callers cannot accidentally expose those structures.
    """

    if details is None:
        return {}
    if not isinstance(details, dict):
        raise TypeError("public details must be a dictionary")
    return {
        _check_sensitive_key(key, "details"): _copy_safe_detail_value(
            value,
            f"details.{key}",
            set(),
        )
        for key, value in details.items()
    }
