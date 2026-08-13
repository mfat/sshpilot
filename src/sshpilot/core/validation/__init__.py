"""Connection and forwarding validation (GTK-free)."""
from .connection import SSHConnectionValidator, ValidationResult

__all__ = ["SSHConnectionValidator", "ValidationResult"]
