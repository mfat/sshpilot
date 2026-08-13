"""Compatibility shim — validation lives in ``sshpilot.core.validation.connection``."""
from sshpilot.core.validation.connection import (  # noqa: F401
    SSHConnectionValidator,
    ValidationResult,
)

__all__ = ["SSHConnectionValidator", "ValidationResult"]
