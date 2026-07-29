"""Strict remote POSIX path validation for daemon SFTP operations."""

from __future__ import annotations

from typing import Any


class RemotePathError(ValueError):
    """Raised when a remote path violates protocol safety rules."""


def validate_remote_path(value: Any, *, field_name: str = "remote path") -> str:
    """Validate and return one remote path string.

    Paths are treated as opaque POSIX-style strings. Local ``os.path``
    semantics are never applied. NUL is rejected; empty strings are
    rejected. Leading dashes and shell metacharacters are preserved as
    literal path characters — callers must never interpolate them into a
    shell.
    """

    if type(value) is not str:
        raise RemotePathError(f"{field_name} must be a string")
    if not value:
        raise RemotePathError(f"{field_name} must not be empty")
    if "\x00" in value:
        raise RemotePathError(f"{field_name} must not contain NUL")
    if len(value) > 4096:
        raise RemotePathError(f"{field_name} exceeds maximum length")
    return value


def is_absolute_remote_path(path: str) -> bool:
    """Return True when ``path`` begins with ``/``."""

    return validate_remote_path(path).startswith("/")


def remote_path_basename(path: str) -> str:
    """Return the final component of a remote path without local semantics."""

    text = validate_remote_path(path)
    if text == "/":
        return "/"
    text = text.rstrip("/")
    if not text:
        return "/"
    index = text.rfind("/")
    if index < 0:
        return text
    return text[index + 1 :] or "/"


def remote_path_dirname(path: str) -> str:
    """Return the parent directory of a remote path."""

    text = validate_remote_path(path)
    if text == "/":
        return "/"
    text = text.rstrip("/")
    index = text.rfind("/")
    if index < 0:
        return "."
    if index == 0:
        return "/"
    return text[:index]


def remote_path_join(base: str, name: str) -> str:
    """Join a remote directory and a single name component safely."""

    root = validate_remote_path(base)
    child = validate_remote_path(name, field_name="remote name")
    if "/" in child or child in (".", ".."):
        # Allow ".." and "." only as deliberate relative names when they are
        # the entire component; nested separators in ``name`` are rejected so
        # callers cannot smuggle multi-segment escapes through join.
        if "/" in child:
            raise RemotePathError("remote name must not contain path separators")
    if root.endswith("/"):
        return f"{root}{child}"
    return f"{root}/{child}"
