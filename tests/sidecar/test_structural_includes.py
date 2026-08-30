"""Structural SSH config mutations: Include trees, nesting, and reordering.

Any ``Include`` directive anywhere in a config snapshot disables trustworthy
destination evidence for the *entire* snapshot (see
``ssh_config_loader._static_identity_evidence``'s ``global_reason``), so a
config tree that uses Include can only ever preserve identity through exact
alias continuity (Rule 1) -- destination-based reconciliation (Rule 2 and
below) is unavailable. This module holds that constant and checks structural
mutations that keep aliases unchanged (moving a Host block between the root
and an included file, reordering/renaming Includes, nesting Includes) never
disturb identity or the effective OpenSSH configuration, using the same
invariants as ``tests/sidecar/state_machine.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.connections.state_file import read_identity_state_v2

from .assertions import (
    AliasOwnership,
    active_alias_state,
    check_exact_alias_continuity,
    check_state_invariants,
)
from .model import LogicalConnection, marker_for, render_tree

_SSH_AVAILABLE = shutil.which("ssh") is not None

_SOURCES = ("config", "conf.d/a.conf", "conf.d/b.conf", "conf.d/nested/c.conf")


def _connections_strategy():
    aliases = st.lists(
        # A purely-numeric token (e.g. "000") is parsed by OpenSSH itself as
        # an IPv4 literal (inet_aton-style shorthand) once it goes through
        # ssh -G, independent of anything sshPilot does -- confirmed by
        # reproducing "hostname 0.0.0.0" for Host 000 with plain OpenSSH.
        # Requiring one letter keeps the ssh -G oracle meaningful.
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=3, max_size=8).filter(
            lambda value: not value.isdigit()
        ),
        min_size=2,
        max_size=5,
        unique=True,
    )

    def build(alias_list):
        return [
            LogicalConnection(
                logical_id=f"L{index}",
                aliases=(alias,),
                hostname=f"{alias}.example",
                port=22,
                username="deploy",
                source=_SOURCES[index % len(_SOURCES)],
            )
            for index, alias in enumerate(alias_list)
        ]

    return aliases.map(build)


_MOVE = "move_source"
_REORDER = "reorder"
_RENAME_INCLUDE_FILE = "rename_include_file"


def _mutations_strategy():
    return st.lists(
        st.one_of(
            st.tuples(st.just(_MOVE), st.integers(min_value=0, max_value=99), st.sampled_from(_SOURCES)),
            st.tuples(st.just(_REORDER), st.integers(min_value=0, max_value=99), st.just(None)),
            st.tuples(st.just(_RENAME_INCLUDE_FILE), st.integers(min_value=0, max_value=99), st.just(None)),
        ),
        max_size=8,
    )


def _write_tree(root_dir: Path, connections: List[LogicalConnection]) -> None:
    # Absolute Include paths: see render_tree's docstring for why a relative
    # one would make real OpenSSH silently drop every included Host block
    # for a root config that isn't ~/.ssh/config.
    files = render_tree(connections, include_base=str(root_dir))
    for relpath, text in files.items():
        path = root_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _ssh_g_hostname(config: Path, alias: str) -> str | None:
    if not _SSH_AVAILABLE:
        return None
    try:
        completed = subprocess.run(
            ["ssh", "-G", "-F", str(config), "-o", "CanonicalizeHostname=no", alias],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in completed.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname":
            return value
    return None


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.data_too_large, HealthCheck.function_scoped_fixture],
)
@given(connections=_connections_strategy(), mutations=_mutations_strategy())
def test_structural_moves_preserve_alias_identity_and_ssh_semantics(connections, mutations):
    with tempfile.TemporaryDirectory(prefix="sshpilot-sidecar-tree-") as tmp:
        root_dir = Path(tmp)
        root = root_dir / "config"
        _write_tree(root_dir, connections)
        state_path = root_dir / "connections.json"
        repo = ConnectionRepository(
            ssh_store=SshConfigStore(root, isolated=True),
            state_path=state_path,
            legacy_config_path=root_dir / "legacy.json",
            isolated=True,
        )

        ownership = AliasOwnership()
        for connection in connections:
            repo.set_display_name(connection.alias, marker_for(connection.logical_id))
            ownership.bind(connection.alias, connection.logical_id)

        def check():
            state = read_identity_state_v2(state_path)
            check_state_invariants(state, ownership)
            return active_alias_state(state)

        last_alias_state = check()
        pre_hostnames = {
            connection.alias: _ssh_g_hostname(root, connection.alias) for connection in connections
        }

        for kind, pick, arg in mutations:
            if not connections:
                break
            connection = connections[pick % len(connections)]
            if kind == _MOVE:
                connection.source = arg
            elif kind == _REORDER:
                connections = connections[pick % len(connections):] + connections[: pick % len(connections)]
            elif kind == _RENAME_INCLUDE_FILE:
                old_source = connection.source
                if old_source == "config":
                    continue
                new_source = old_source.replace(".conf", "-renamed.conf")
                for other in connections:
                    if other.source == old_source:
                        other.source = new_source
                stale = root_dir / old_source
                if stale.exists():
                    stale.unlink()

            _write_tree(root_dir, connections)
            repo.reload()
            after_alias_state = check()
            check_exact_alias_continuity(last_alias_state, after_alias_state)
            last_alias_state = after_alias_state

        for connection in connections:
            after_hostname = _ssh_g_hostname(root, connection.alias)
            before_hostname = pre_hostnames.get(connection.alias)
            if before_hostname is not None and after_hostname is not None:
                assert after_hostname == before_hostname, (
                    f"structural move changed ssh -G effective hostname for "
                    f"{connection.alias!r}: {before_hostname!r} -> {after_hostname!r}"
                )

        # Reload of an unchanged tree must be a stable no-op (idempotence).
        before_generation = read_identity_state_v2(state_path).sidecar_generation
        repo.reload()
        after_generation = read_identity_state_v2(state_path).sidecar_generation
        assert before_generation == after_generation
