"""Materialize an SSH config tree with config-static port forwards removed.

Daemon-owned ``ssh -N -L/-R/-D`` launches must not inherit Host-level
``LocalForward`` / ``RemoteForward`` / ``DynamicForward`` from the user's
config. Those bind ports (e.g. ``DynamicForward localhost:7000``) collide with
an already-open terminal session and, with ``ExitOnForwardFailure=yes``, make
the dedicated forward process exit 255 immediately.

``ClearAllForwardings=yes`` cannot be used on the same argv as ``-L``/``-R``/
``-D``: on current OpenSSH it clears the ad-hoc flags as well. The documented
approach is a temporary ``-F`` config that strips only those three directives
(and rewrites ``Include`` paths to stripped copies).
"""

from __future__ import annotations

import glob
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

_FORWARD_DIRECTIVES = frozenset(
    {"localforward", "remoteforward", "dynamicforward"}
)
_INCLUDE_RE = re.compile(r"^(\s*)([Ii][Nn][Cc][Ll][Uu][Dd][Ee])(\s*=\s*|\s+)(.+?)\s*$")
_DIRECTIVE_RE = re.compile(r"^(\s*)(\S+)(.*)$")

# Env key the forward process runner pops before Popen and deletes on exit.
FORWARD_SSH_CONFIG_ROOT_ENV = "SSHPILOT_FORWARD_SSH_CONFIG_ROOT"


def default_user_ssh_config_path() -> str:
    return os.path.expanduser("~/.ssh/config")


def materialize_ssh_config_without_port_forwards(
    source_config: Optional[str] = None,
    *,
    destination_dir: Optional[str] = None,
) -> str:
    """Return path to a root ``-F`` config with port-forward directives removed.

    When ``source_config`` is missing or empty, writes a minimal empty config
    so OpenSSH still has a valid ``-F`` target (Host options then come only
    from the command line / other ``-o`` flags).
    """

    source = (source_config or "").strip() or default_user_ssh_config_path()
    source_path = Path(os.path.expanduser(source)).resolve()
    if destination_dir is None:
        root = Path(
            tempfile.mkdtemp(
                prefix=f"sshpilot-fwd-cfg-{os.getuid()}-",
            )
        )
    else:
        root = Path(destination_dir)
        root.mkdir(parents=True, exist_ok=True)

    mapping: Dict[str, str] = {}
    seen: Set[str] = set()
    root_out = root / "config"
    if source_path.is_file():
        _materialize_file(source_path, root_out, root, mapping, seen)
    else:
        root_out.write_text(
            "# sshpilot: no source ssh config; daemon forward launch\n",
            encoding="utf-8",
        )
        try:
            os.chmod(root_out, 0o600)
        except OSError:
            pass
    return str(root_out)


def _materialize_file(
    source: Path,
    destination: Path,
    tree_root: Path,
    mapping: Dict[str, str],
    seen: Set[str],
) -> None:
    key = str(source)
    if key in seen:
        return
    seen.add(key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read SSH config %s: %s", source, exc)
        destination.write_text(
            f"# sshpilot: unreadable source {source}\n",
            encoding="utf-8",
        )
        return

    out_lines: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        newline = ""
        line = raw_line
        if raw_line.endswith("\r\n"):
            newline = "\r\n"
            line = raw_line[:-2]
        elif raw_line.endswith("\n"):
            newline = "\n"
            line = raw_line[:-1]

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw_line if raw_line.endswith(("\n", "\r\n")) else line + newline)
            continue

        include_match = _INCLUDE_RE.match(line)
        if include_match is not None:
            prefix, keyword, sep, pattern = include_match.groups()
            rewritten = _rewrite_include(
                pattern.strip().strip('"'),
                source.parent,
                tree_root,
                mapping,
                seen,
            )
            out_lines.append(f"{prefix}{keyword}{sep}{rewritten}{newline}")
            continue

        directive_match = _DIRECTIVE_RE.match(line)
        if directive_match is not None:
            token = directive_match.group(2).split("=", 1)[0].lower()
            if token in _FORWARD_DIRECTIVES:
                continue
        out_lines.append(line + newline)

    destination.write_text("".join(out_lines), encoding="utf-8")
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    mapping[key] = str(destination)


def _rewrite_include(
    pattern: str,
    base_dir: Path,
    tree_root: Path,
    mapping: Dict[str, str],
    seen: Set[str],
) -> str:
    expanded = os.path.expanduser(pattern)
    if not os.path.isabs(expanded):
        expanded = str((base_dir / expanded).resolve())
    matches = sorted(glob.glob(expanded)) or (
        [expanded] if os.path.isfile(expanded) else []
    )
    rewritten_paths: list[str] = []
    for match in matches:
        src = Path(match).resolve()
        if not src.is_file():
            continue
        if str(src) in mapping:
            rewritten_paths.append(mapping[str(src)])
            continue
        # Keep a stable relative layout under tree_root when possible.
        try:
            rel = src.relative_to(Path.home())
            dest = tree_root / "home" / rel
        except ValueError:
            try:
                rel = src.relative_to(base_dir)
                dest = tree_root / "inc" / rel
            except ValueError:
                digest = abs(hash(str(src))) % (10**12)
                dest = tree_root / "abs" / f"{digest}-{src.name}"
        _materialize_file(src, dest, tree_root, mapping, seen)
        rewritten_paths.append(str(dest))
    if not rewritten_paths:
        # Preserve a path OpenSSH can miss quietly rather than invent hosts.
        return expanded
    return " ".join(rewritten_paths)


def argv_config_file(argv: list[str] | tuple[str, ...]) -> Optional[str]:
    """Return the ``-F`` config path from *argv*, if present."""

    items = list(argv)
    for index, item in enumerate(items):
        if item == "-F" and index + 1 < len(items):
            return items[index + 1]
        if item.startswith("-F") and len(item) > 2:
            return item[2:]
    return None


def argv_with_config_file(
    argv: list[str] | tuple[str, ...], config_path: str
) -> tuple[str, ...]:
    """Return argv with ``-F config_path`` replacing any existing ``-F``."""

    items = list(argv)
    if not items:
        return ("ssh", "-F", config_path)
    index = 1
    while index < len(items):
        item = items[index]
        if item == "-F" and index + 1 < len(items):
            items[index + 1] = config_path
            return tuple(items)
        if item.startswith("-F") and len(item) > 2:
            items[index] = f"-F{config_path}"
            return tuple(items)
        index += 1
    return (items[0], "-F", config_path, *items[1:])
