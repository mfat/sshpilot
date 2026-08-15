"""Compatibility facade for SSH config editing and canonical resolution.

Effective-config and Include behavior lives in
``sshpilot.core.ssh_config_effective``.  This module remains import-compatible
for editor, backup, and older plugin callers; it contains only the specialized
atomic write/validation helpers below.  Remove the facade after the documented
compatibility window once downstream imports have migrated.
"""

from __future__ import annotations

import logging
import os
import stat
import shutil
import subprocess
import tempfile
from typing import Optional

from .core.ssh_config_effective import (
    SSHConfigPathDiscovery,
    collect_host_block_lines,
    discover_ssh_config_paths,
    diff_effective_config,
    expand_ssh_tokens,
    get_effective_ssh_config,
    resolve_ssh_config_files,
)

logger = logging.getLogger(__name__)


def __getattr__(name):
    """Resolve private legacy test/helper names without a second implementation."""
    if name in {"_authored_directives", "_effective_config_lines"}:
        import importlib

        module = importlib.import_module(
            ".core.ssh_config_effective", package=__package__
        )
        return getattr(module, name)
    raise AttributeError(name)


def atomic_write_text(
    path: str,
    text: str,
    *,
    mode: Optional[int] = None,
    backup: bool = False,
) -> None:
    """Atomically write editor text, preserving permissions when requested."""
    directory = os.path.dirname(path) or "."
    exists = os.path.exists(path)
    final_mode = mode
    if final_mode is None and exists:
        try:
            final_mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            final_mode = None
    if backup and exists:
        try:
            shutil.copy2(path, f"{path}.bak")
            if final_mode is not None:
                os.chmod(f"{path}.bak", final_mode)
        except Exception as exc:  # noqa: BLE001 - backup is best effort
            logger.warning("Could not back up %s before writing: %s", path, exc)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".sshpilot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if final_mode is not None:
        try:
            os.chmod(path, final_mode)
        except OSError as exc:
            logger.debug("Unable to set permissions on %s: %s", path, exc)


def validate_ssh_config_text(text: str) -> Optional[str]:
    """Validate editor text with OpenSSH using a throwaway config file."""
    fd, tmp_path = tempfile.mkstemp(prefix=".sshpilot-validate-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        result = subprocess.run(
            ["ssh", "-G", "-F", tmp_path, "sshpilot-config-check"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    except Exception as exc:  # noqa: BLE001 - validation is best effort
        logger.debug("ssh -G validation could not run: %s", exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if result.returncode == 0:
        return None
    error = (result.stderr or "").strip()
    for line in error.splitlines():
        if "line " in line or "Bad " in line or "error" in line.lower():
            return line.strip()
    return error.splitlines()[-1].strip() if error else f"ssh -G exited {result.returncode}"


__all__ = [
    "SSHConfigPathDiscovery",
    "atomic_write_text",
    "collect_host_block_lines",
    "diff_effective_config",
    "discover_ssh_config_paths",
    "expand_ssh_tokens",
    "get_effective_ssh_config",
    "resolve_ssh_config_files",
    "validate_ssh_config_text",
]
