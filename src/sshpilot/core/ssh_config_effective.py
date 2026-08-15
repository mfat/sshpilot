"""Canonical GTK-free OpenSSH configuration resolution for the daemon core.

This module owns Include discovery, ``ssh -G`` execution, repeated-option
parsing, and authored/effective comparison.  The legacy top-level
``ssh_config_utils`` module is only a compatibility facade for these read
helpers plus its unrelated atomic editor/validation helpers.
"""

from __future__ import annotations

import glob
import getpass
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Union

from ..ssh_config_document import SSHConfigDocument

logger = logging.getLogger(__name__)

_PATH_DIRECTIVES = frozenset(
    {
        "identityfile",
        "certificatefile",
        "identityagent",
        "pkcs11provider",
        "securitykeyprovider",
        "controlpath",
        "userknownhostsfile",
        "globalknownhostsfile",
        "xauthlocation",
        "revokedhostkeys",
        "include",
    }
)
_TOKEN_RE = re.compile(r"%(.)")


def expand_ssh_tokens(value: str) -> str:
    """Expand host-independent OpenSSH tokens used by Include paths."""
    if not value or "%" not in value:
        return value
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    mapping = {
        "%": "%",
        "d": os.path.expanduser("~"),
        "u": user,
        "i": str(os.getuid()) if hasattr(os, "getuid") else "",
        "l": hostname,
        "L": hostname.split(".", 1)[0],
    }
    return _TOKEN_RE.sub(lambda match: mapping.get(match.group(1), match.group(0)), value)


@dataclass(frozen=True)
class SSHConfigPathDiscovery:
    """Resolved SSH inputs and filesystem paths needed to rediscover them."""

    files: tuple[str, ...]
    watch_paths: FrozenSet[str]
    unreadable_paths: FrozenSet[str]


def _watch_parent_for_pattern(pattern: str) -> str:
    candidate = pattern
    while glob.has_magic(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    if not candidate or glob.has_magic(candidate):
        candidate = os.path.dirname(pattern) or "."
    return os.path.abspath(candidate)


def discover_ssh_config_paths(
    main_path: str, *, max_depth: int = 32
) -> SSHConfigPathDiscovery:
    """Resolve the Include graph and retain parents for future glob matches."""
    resolved: List[str] = []
    visited: Set[str] = set()
    watch_paths: Set[str] = set()
    unreadable_paths: Set[str] = set()

    def resolve(path: str, depth: int, stack: List[str]) -> None:
        absolute = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        watch_paths.add(absolute)
        if absolute in stack:
            logger.warning("Include cycle detected: %s -> %s", " -> ".join(stack), absolute)
            return
        if depth > max_depth:
            logger.warning("Maximum include depth (%d) exceeded at %s", max_depth, absolute)
            return
        if absolute in visited:
            return
        try:
            with open(absolute, encoding="utf-8") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeError) as exc:
            logger.warning("Cannot read SSH config include %s: %s", absolute, exc)
            if os.path.exists(absolute):
                unreadable_paths.add(absolute)
            return
        visited.add(absolute)
        resolved.append(absolute)
        base_dir = os.path.dirname(absolute)
        stack.append(absolute)
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.lower().startswith("include "):
                continue
            for pattern in shlex.split(line[len("include "):]):
                expanded = os.path.expanduser(
                    os.path.expandvars(expand_ssh_tokens(pattern))
                )
                if not os.path.isabs(expanded):
                    expanded = os.path.abspath(os.path.join(base_dir, expanded))
                else:
                    expanded = os.path.abspath(expanded)
                if glob.has_magic(expanded):
                    watch_paths.add(_watch_parent_for_pattern(expanded))
                else:
                    watch_paths.add(expanded)
                    watch_paths.add(os.path.dirname(expanded) or ".")
                for matched in sorted(glob.glob(expanded)):
                    if os.path.isdir(matched):
                        watch_paths.add(os.path.abspath(matched))
                        for child in sorted(glob.glob(os.path.join(matched, "*"))):
                            resolve(child, depth + 1, stack)
                    else:
                        resolve(matched, depth + 1, stack)
        stack.pop()

    resolve(main_path, 1, [])
    return SSHConfigPathDiscovery(
        files=tuple(resolved),
        watch_paths=frozenset(watch_paths),
        unreadable_paths=frozenset(unreadable_paths),
    )


def resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    return list(discover_ssh_config_paths(main_path, max_depth=max_depth).files)


def _resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    """Private compatibility name used by older core tests."""
    return resolve_ssh_config_files(main_path, max_depth=max_depth)


def _safe_host_identifier(host: str) -> str:
    value = (host or "").strip()
    if not value or "\x00" in value or value.startswith("-"):
        return ""
    return value


def collect_host_block_lines(
    host_identifier: str, ssh_config_path: Optional[str] = None
) -> List[str]:
    host_identifier = _safe_host_identifier(host_identifier)
    if not host_identifier:
        return []
    config_path = ssh_config_path or os.path.expanduser("~/.ssh/config")
    try:
        files = resolve_ssh_config_files(config_path)
    except Exception:
        files = [config_path]
    combined: List[str] = []
    for path in files:
        try:
            if not path or not os.path.exists(path):
                continue
            document = SSHConfigDocument.parse_file(path)
        except (OSError, UnicodeDecodeError):
            continue
        for block in document.host_blocks(host_identifier):
            combined.extend(line.rstrip("\r\n") for line in block.lines)
    return combined


def get_effective_ssh_config(
    host: str,
    config_file: Optional[str] = None,
    *,
    user: Optional[str] = None,
    port: Optional[int] = None,
    proxy_jump: Optional[str] = None,
) -> Dict[str, Union[str, List[str]]]:
    """Resolve *host* through OpenSSH without allowing option confusion."""
    host = _safe_host_identifier(host)
    if not host:
        return {}
    command = ["ssh"]
    if config_file:
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(config_file)))
        if not os.path.isfile(expanded):
            logger.warning("Requested SSH config override %s does not exist", expanded)
            return {}
        command.extend(["-F", expanded])
    if user:
        command.extend(["-o", f"User={user}"])
    if port is not None:
        command.extend(["-p", str(port)])
    if proxy_jump:
        command.extend(["-o", f"ProxyJump={proxy_jump}"])
    command.extend(["-G", host])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=10
        )
    except Exception:
        return {}
    config: Dict[str, Union[str, List[str]]] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        key = key.lower()
        value = value.strip()
        previous = config.get(key)
        if previous is None:
            config[key] = value
        elif isinstance(previous, list):
            previous.append(value)
        else:
            config[key] = [previous, value]
    return config


def _effective_config_lines(config: Dict[str, Union[str, List[str]]]) -> List[str]:
    lines: List[str] = []
    for key in sorted(config):
        values = config[key]
        if isinstance(values, list):
            lines.extend(f"{key} {value}" for value in values)
        else:
            lines.append(f"{key} {values}")
    return lines


def _authored_directives(block_text: str) -> Set[str]:
    names: Set[str] = set()
    for raw_line in block_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        token = re.split(r"[\s=]", line, maxsplit=1)[0].lower()
        if token not in {"host", "match"}:
            names.add(token)
    return names


def diff_effective_config(
    host: str, config_file: Optional[str], own_block_text: str
) -> Optional[Dict[str, object]]:
    """Compare an authored block against the daemon-selected OpenSSH config."""
    if not _safe_host_identifier(host):
        return None
    fd, temporary_path = tempfile.mkstemp(prefix=".sshpilot-own-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(own_block_text)
        own = get_effective_ssh_config(host, config_file=temporary_path)
        if config_file is None:
            user_config = os.path.expanduser("~/.ssh/config")
            compare_file = user_config if os.path.isfile(user_config) else temporary_path
            full = get_effective_ssh_config(host, config_file=compare_file)
            display = get_effective_ssh_config(host)
        else:
            full = get_effective_ssh_config(host, config_file=config_file)
            display = full
    finally:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
    if not own or not full:
        return None
    display = display or full

    def expand_paths(config):
        result = {}
        for key, value in config.items():
            if key not in _PATH_DIRECTIVES:
                result[key] = value
            elif isinstance(value, list):
                result[key] = [os.path.expanduser(item) for item in value]
            elif isinstance(value, str):
                result[key] = os.path.expanduser(value)
            else:
                result[key] = value
        return result

    full, own, display = map(expand_paths, (full, own, display))

    def as_list(value) -> List[str]:
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    authored = _authored_directives(own_block_text)
    changes: List[Dict[str, object]] = []
    for key in sorted(set(full) | set(own)):
        own_values = as_list(own.get(key))
        full_values = as_list(full.get(key))
        if own_values == full_values:
            continue
        if key not in authored:
            changes.append(
                {
                    "key": key,
                    "own": [],
                    "effective": full_values,
                    "added": full_values,
                    "removed": [],
                    "kind": "added",
                }
            )
            continue
        own_counts = Counter(own_values)
        full_counts = Counter(full_values)
        added = list((full_counts - own_counts).elements())
        removed = list((own_counts - full_counts).elements())
        kind = (
            "overridden"
            if added and removed
            else "added"
            if added
            else "removed"
            if removed
            else "overridden"
        )
        changes.append(
            {
                "key": key,
                "own": own_values,
                "effective": full_values,
                "added": added,
                "removed": removed,
                "kind": kind,
            }
        )
    return {
        "has_diff": bool(changes),
        "changes": changes,
        "own": _effective_config_lines(own),
        "full": _effective_config_lines(display),
    }
