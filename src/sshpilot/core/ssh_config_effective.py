"""GTK-free effective OpenSSH configuration comparison for the daemon core.

The legacy top-level ``ssh_config_utils`` module remains available to direct
utility tests and older integrations.  Production core code uses this module
so the daemon service does not depend on the legacy/frontend-facing module.
"""

from __future__ import annotations

import os
import glob
import logging
import re
import shlex
import subprocess
import tempfile
from collections import Counter
from typing import Dict, List, Optional, Set, Union

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


def _expand_include_token(value: str) -> str:
    """Expand the local-only tokens supported by OpenSSH Include paths."""
    if not value or "%" not in value:
        return value
    mapping = {
        "%": "%",
        "d": os.path.expanduser("~"),
        "u": os.environ.get("USER", ""),
        "i": str(os.getuid()) if hasattr(os, "getuid") else "",
        "l": os.uname().nodename if hasattr(os, "uname") else "",
    }
    hostname = mapping["l"]
    mapping["L"] = hostname.split(".", 1)[0]
    return re.sub(r"%(.)", lambda match: mapping.get(match.group(1), match.group(0)), value)


def _resolve_ssh_config_files(main_path: str, *, max_depth: int = 32) -> List[str]:
    """Resolve the Include graph without depending on the legacy utility module."""
    resolved: List[str] = []
    visited: Set[str] = set()

    def resolve(path: str, depth: int, stack: List[str]) -> None:
        absolute = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
        if absolute in stack or depth > max_depth or absolute in visited:
            return
        try:
            with open(absolute, encoding="utf-8") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeError) as exc:
            logger.debug("Cannot read SSH config include %s: %s", absolute, exc)
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
                expanded = os.path.expanduser(os.path.expandvars(_expand_include_token(pattern)))
                if not os.path.isabs(expanded):
                    expanded = os.path.join(base_dir, expanded)
                for matched in sorted(glob.glob(os.path.abspath(expanded))):
                    if os.path.isdir(matched):
                        for child in sorted(glob.glob(os.path.join(matched, "*"))):
                            resolve(child, depth + 1, stack)
                    else:
                        resolve(matched, depth + 1, stack)
        stack.pop()

    resolve(main_path, 1, [])
    return resolved


def collect_host_block_lines(
    host_identifier: str, ssh_config_path: Optional[str] = None
) -> List[str]:
    """Return combined lines from every concrete matching Host stanza."""
    host_identifier = (host_identifier or "").strip()
    if not host_identifier:
        return []
    config_path = ssh_config_path or os.path.expanduser("~/.ssh/config")
    try:
        files = _resolve_ssh_config_files(config_path)
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
    host: str, config_file: Optional[str] = None
) -> Dict[str, Union[str, List[str]]]:
    """Resolve *host* with OpenSSH ``-G`` and preserve repeated values."""
    command = ["ssh"]
    if config_file:
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(config_file)))
        if not os.path.isfile(expanded):
            return {}
        command.extend(["-F", expanded])
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


def _effective_config_lines(
    config: Dict[str, Union[str, List[str]]]
) -> List[str]:
    lines: List[str] = []
    for key in sorted(config):
        value = config[key]
        if isinstance(value, list):
            lines.extend(f"{key} {item}" for item in value)
        else:
            lines.append(f"{key} {value}")
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
    """Compare the authored Host block with OpenSSH's effective values."""
    if not host:
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

    def expand(config):
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

    full, own, display = expand(full), expand(own), expand(display)

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
        kind = "overridden" if added and removed else "added" if added else "removed" if removed else "overridden"
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
