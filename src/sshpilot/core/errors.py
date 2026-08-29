"""Structured core error codes (UI-agnostic).

GTK and CLI render human-facing messages separately. Domain code raises
:class:`CoreError` (or returns results carrying these codes) instead of
presenting dialogs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFIG_PARSE_ERROR = "CONFIG_PARSE_ERROR"
    KEY_NOT_FOUND = "KEY_NOT_FOUND"
    KEY_INVALID = "KEY_INVALID"
    KEY_EXISTS = "KEY_EXISTS"
    KNOWN_HOST_CONFLICT = "KNOWN_HOST_CONFLICT"
    KNOWN_HOST_IO_ERROR = "KNOWN_HOST_IO_ERROR"
    CONNECTION_STATE_IO_ERROR = "CONNECTION_STATE_IO_ERROR"
    STALE_CONNECTION_STATE = "STALE_CONNECTION_STATE"
    MUTATION_AMBIGUOUS = "MUTATION_AMBIGUOUS"
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
    CONNECTION_ALREADY_EXISTS = "CONNECTION_ALREADY_EXISTS"
    SECRET_BACKEND_UNAVAILABLE = "SECRET_BACKEND_UNAVAILABLE"
    SECRET_LOCKED = "SECRET_LOCKED"
    SECRET_INTERACTION_REQUIRED = "SECRET_INTERACTION_REQUIRED"
    SECRET_PERMISSION_DENIED = "SECRET_PERMISSION_DENIED"
    SECRET_MIGRATION_REQUIRED = "SECRET_MIGRATION_REQUIRED"
    PLUGIN_INCOMPATIBLE = "PLUGIN_INCOMPATIBLE"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    IMPORT_ERROR = "IMPORT_ERROR"
    EXPORT_ERROR = "EXPORT_ERROR"


@dataclass
class CoreError(Exception):
    """Structured domain error with a stable code."""

    code: ErrorCode
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    diagnostic_category: str = "io_error"
    diagnostic_reason: str = "operation rejected"

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.message:
            return f"{self.code.value}: {self.message}"
        return self.code.value


@dataclass(frozen=True)
class FieldError:
    """One field-level validation failure (no UI string required as primary)."""

    field: str
    code: ErrorCode = ErrorCode.VALIDATION_ERROR
    message: str = ""
    severity: str = "error"  # error | warning | info
    reason: str = ""
