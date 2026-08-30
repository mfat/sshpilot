"""Test-only logical connection model.

This module intentionally never reimplements sshPilot's identity matcher
(``sshpilot.core.connections.identity_reconciliation``). It only tracks what
SSH configuration text the harness itself authored, so invariant checks can
compare "what the test asked for" against "what the sidecar actually did"
without re-deriving the production matching decision.

Each :class:`LogicalConnection` is tagged with a globally unique
``display_name`` marker (``DISPLAY::<logical_id>``) at creation. The
production contract is that a display name follows UUID continuity and is
never cleared by ordinary SSH directive edits (see
``docs/architecture/connection-identity-persistence.md``), so that marker is
the tracer the harness uses to catch metadata silently migrating to the
wrong identity.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

DISPLAY_MARKER_PREFIX = "DISPLAY::"


def marker_for(logical_id: str) -> str:
    return f"{DISPLAY_MARKER_PREFIX}{logical_id}"


def logical_id_from_marker(display_name: str) -> Optional[str]:
    if isinstance(display_name, str) and display_name.startswith(DISPLAY_MARKER_PREFIX):
        return display_name[len(DISPLAY_MARKER_PREFIX):]
    return None


@dataclass
class LogicalConnection:
    """A connection the test harness believes should remain one identity.

    ``aliases`` holds every ``Host`` token sharing one declaration (a
    multi-alias ``Host a b`` block); the loader materializes one concrete
    projection per token, so most single-identity mutations use only
    ``aliases[0]``.
    """

    logical_id: str
    aliases: Tuple[str, ...]
    hostname: str
    port: int = 22
    username: str = ""
    identity_files: Tuple[str, ...] = ()
    source: str = "config"

    @property
    def alias(self) -> str:
        return self.aliases[0]

    def render_host_block(self) -> str:
        host_line = "Host " + " ".join(self.aliases)
        lines = [host_line, f"    HostName {self.hostname}"]
        if self.username:
            lines.append(f"    User {self.username}")
        lines.append(f"    Port {self.port}")
        for identity_file in self.identity_files:
            lines.append(f"    IdentityFile {identity_file}")
        return "\n".join(lines) + "\n"

    def clone(self) -> "LogicalConnection":
        return copy.deepcopy(self)


def render_flat_config(connections: List[LogicalConnection]) -> str:
    """Render one flat config file: one concrete ``Host`` block per connection.

    No ``Include``, ``Match``, or wildcard ``Host`` is ever emitted here --
    any of those disables trustworthy destination evidence for the *entire*
    loader snapshot (see ``ssh_config_loader._static_identity_evidence``'s
    ``global_reason``), which would collapse every destination-based
    reconciliation rule down to exact-alias-only. That structural interaction
    is exercised separately in ``tests/sidecar/test_structural_includes.py``.
    """

    return "\n".join(connection.render_host_block() for connection in connections)


def render_tree(
    connections: List[LogicalConnection],
    *,
    root_relpath: str = "config",
    include_base: Optional[str] = None,
) -> Dict[str, str]:
    """Render a config tree keyed by relative path, honoring ``source``.

    Every distinct directory holding a non-root ``source`` gets one
    ``Include <dir>/*.conf`` line in the root file (so files sharing a
    directory -- e.g. ``conf.d/hosts.conf`` and ``conf.d/prod.conf`` -- are
    reachable through one glob, and ``conf.d/nested/*.conf`` is reached via
    its own line, exercising nested Include resolution).

    ``include_base``, when given, is prepended to make every Include
    directive absolute. sshPilot's own loader resolves a relative Include
    relative to the *including file's* directory
    (``ssh_config_loader._resolve_config_files``'s ``base_dir``), but real
    OpenSSH's relative-Include resolution for a ``-F`` file outside
    ``~/.ssh`` does not match that (confirmed empirically: a plain
    ``Include conf.d/*.conf`` silently matches nothing under ``ssh -G -F``
    once the root config isn't ``~/.ssh/config`, regardless of CWD). Pass the
    tree's own root directory here whenever the rendered tree needs to be a
    fair ``ssh -G`` oracle target; omit it to match ordinary in-app usage.
    """

    import posixpath

    by_source: Dict[str, List[LogicalConnection]] = {}
    for connection in connections:
        by_source.setdefault(connection.source, []).append(connection)

    files: Dict[str, str] = {}
    directories: List[str] = []
    for source in sorted(by_source):
        if source == root_relpath:
            continue
        files[source] = render_flat_config(by_source[source])
        directory = posixpath.dirname(source)
        if directory not in directories:
            directories.append(directory)

    prefix = f"{include_base}/" if include_base else ""
    root_lines: List[str] = []
    for directory in sorted(directories):
        root_lines.append(f"Include {prefix}{directory}/*.conf")
    if directories:
        root_lines.append("")
    root_lines.append(render_flat_config(by_source.get(root_relpath, [])))
    files[root_relpath] = "\n".join(root_lines)
    return files
