"""File-manager-specific exception types."""

from __future__ import annotations


class TransferCancelledException(Exception):
    """Exception raised when a transfer is cancelled."""
