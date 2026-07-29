"""Shared helpers for SSH key validation and metadata.

Implementation lives in ``sshpilot.core.keys``; this module keeps the
historical private-name exports used across the tree.
"""
from __future__ import annotations

from sshpilot.core.keys import (
    SKIPPED_FILENAMES as _SKIPPED_FILENAMES,
    is_private_key as _is_private_key,
    looks_like_private_key as _looks_like_private_key,
)

__all__ = ["_SKIPPED_FILENAMES", "_is_private_key", "_looks_like_private_key"]
