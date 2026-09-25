"""Flatpak: import browsed SSH key/cert files into ``~/.ssh``.

Sandbox OpenSSH only has durable filesystem access to ``~/.ssh``. Keys
chosen via the FileChooser portal live under ``/run/user/.../doc/...`` and
must not be written into ssh_config as IdentityFile paths. Copying into
``~/.ssh`` (after user confirmation in the UI) gives a durable host path
that the bundled OpenSSH can read.

GTK-free so unit tests can exercise the filesystem logic without GI.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .platform_utils import get_ssh_dir, is_flatpak

logger = logging.getLogger(__name__)


def needs_flatpak_ssh_import(
    path: str,
    *,
    flatpak: Optional[bool] = None,
    ssh_dir: Optional[str] = None,
) -> bool:
    """Return True when *path* must be copied into ``~/.ssh`` for Flatpak use."""
    if not path or not str(path).strip():
        return False
    if flatpak is None:
        flatpak = is_flatpak()
    if not flatpak:
        return False
    root = Path(os.path.realpath(ssh_dir or get_ssh_dir()))
    try:
        candidate = Path(os.path.realpath(path))
    except OSError:
        candidate = Path(os.path.abspath(path))
    try:
        return not candidate.is_relative_to(root)
    except (ValueError, OSError):
        return True


def companion_paths_for_private_key(private_path: str) -> List[str]:
    """Return existing sidecar files next to a private key (``.pub``, ``-cert.pub``)."""
    if not private_path:
        return []
    # Selecting a public key or cert as the "private" browse target should not
    # invent companions by stripping suffixes.
    base = os.path.basename(private_path)
    if base.endswith("-cert.pub") or base.endswith(".pub"):
        return []
    out: List[str] = []
    for suffix in (".pub", "-cert.pub"):
        sibling = private_path + suffix
        if os.path.isfile(sibling):
            out.append(sibling)
    return out


def allocate_ssh_basename(ssh_dir: str, basename: str) -> str:
    """Pick a free basename under *ssh_dir* (``name``, then ``name-1``, …)."""
    name = basename or "imported_key"
    candidate = name
    n = 0
    while os.path.exists(os.path.join(ssh_dir, candidate)):
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def import_into_ssh_dir(
    source_path: str,
    *,
    ssh_dir: Optional[str] = None,
    companions: Optional[Sequence[str]] = None,
    mode: int = 0o600,
) -> Tuple[str, List[str]]:
    """Copy *source_path* (and optional companions) into ``~/.ssh``.

    Returns ``(primary_dest, companion_dests)``. Raises ``OSError`` on failure.
    Companion basenames are rewritten to match the allocated primary basename
    when they are the usual ``.pub`` / ``-cert.pub`` sidecars of the source.
    """
    src = os.path.realpath(source_path) if os.path.exists(source_path) else source_path
    if not os.path.isfile(src):
        raise FileNotFoundError(f"not a file: {source_path}")

    dest_dir = os.path.realpath(ssh_dir or get_ssh_dir())
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o700)
    except OSError:
        pass

    primary_base = allocate_ssh_basename(dest_dir, os.path.basename(src))
    primary_dest = os.path.join(dest_dir, primary_base)
    _copy_with_mode(src, primary_dest, mode)

    companion_dests: List[str] = []
    source_base = os.path.basename(src)
    for companion in companions if companions is not None else ():
        if not companion or not os.path.isfile(companion):
            continue
        companion_name = os.path.basename(companion)
        dest_name = _rewrite_companion_basename(source_base, primary_base, companion_name)
        dest_name = allocate_ssh_basename(dest_dir, dest_name)
        dest = os.path.join(dest_dir, dest_name)
        _copy_with_mode(companion, dest, mode)
        companion_dests.append(dest)

    logger.info(
        "Imported SSH path into ~/.ssh: %s -> %s (companions=%s)",
        source_path,
        primary_dest,
        companion_dests,
    )
    return primary_dest, companion_dests


def import_private_key_into_ssh_dir(
    source_path: str,
    *,
    ssh_dir: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Import a private key and any present ``.pub`` / ``-cert.pub`` companions."""
    return import_into_ssh_dir(
        source_path,
        ssh_dir=ssh_dir,
        companions=companion_paths_for_private_key(source_path),
    )


def import_certificate_into_ssh_dir(
    source_path: str,
    *,
    ssh_dir: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Import a certificate file only (no private-key companions)."""
    return import_into_ssh_dir(source_path, ssh_dir=ssh_dir, companions=())


def _rewrite_companion_basename(
    source_base: str, allocated_primary: str, companion_name: str
) -> str:
    """Map ``id.pub`` → ``id-1.pub`` when the private key was renamed to ``id-1``."""
    for suffix in (".pub", "-cert.pub"):
        if companion_name == source_base + suffix:
            return allocated_primary + suffix
    return companion_name


def _copy_with_mode(src: str, dest: str, mode: int) -> None:
    shutil.copy2(src, dest)
    os.chmod(dest, mode)


__all__ = [
    "allocate_ssh_basename",
    "companion_paths_for_private_key",
    "import_certificate_into_ssh_dir",
    "import_into_ssh_dir",
    "import_private_key_into_ssh_dir",
    "needs_flatpak_ssh_import",
]
