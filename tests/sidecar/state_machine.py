"""Stateful Hypothesis machine stress-testing sidecar identity reconciliation.

The machine drives the real ``ConnectionRepository`` (production code) through
sequences of managed API calls, raw/external SSH config edits, restarts, and
default/isolated mode switches, and checks the invariants in
``tests/sidecar/assertions.py`` after every step. It never reimplements
sshPilot's identity matcher (``identity_reconciliation.reconcile_identities``)
-- see that module and ``docs/architecture/connection-identity-persistence.md``
for the actual matching contract this harness holds production to.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypothesis import assume
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    multiple,
    precondition,
    rule,
)

from sshpilot.api.models.connections import SaveSshConfigTextRequest
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.connections.state_file import read_identity_state_v2
from sshpilot.core.errors import CoreError, ErrorCode

from .assertions import (
    AliasOwnership,
    InvariantViolation,
    active_alias_state,
    check_exact_alias_continuity,
    check_state_invariants,
)
from .model import LogicalConnection, marker_for, render_flat_config
from .strategies import (
    alias_suffixes,
    group_names,
    hostnames,
    identity_file_lists,
    ports,
    tag_names,
    usernames,
)

_SSH_AVAILABLE = shutil.which("ssh") is not None

# Sentinel returned by ``_mutate_or_tolerate_ambiguity`` when the repository
# refused a mutation because its target alias is still ``pending_ambiguities``.
_AMBIGUOUS = object()


class SidecarIdentityMachine(RuleBasedStateMachine):
    """Drives ``ConnectionRepository`` and checks sidecar identity invariants."""

    connections: Bundle = Bundle("connections")

    def __init__(self) -> None:
        super().__init__()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="sshpilot-sidecar-")
        self.base = Path(self._tmpdir.name)
        self.state_path = self.base / "connections.json"
        self.legacy_path = self.base / "config.json"

        default_root = self.base / "config_default"
        default_root.write_text("", encoding="utf-8")
        self.roots: Dict[bool, Path] = {False: default_root}
        self.models: Dict[bool, Dict[str, LogicalConnection]] = {False: {}}
        self.orders: Dict[bool, List[str]] = {False: []}
        self.current_isolated = False

        self.repo = ConnectionRepository(
            ssh_store=SshConfigStore(default_root, isolated=False),
            state_path=self.state_path,
            legacy_config_path=self.legacy_path,
            isolated=False,
        )

        self.ownership = AliasOwnership()
        self._alias_seq = 0
        self._logical_seq = 0
        self._graveyard: List[str] = []
        self._last_alias_state = active_alias_state(read_identity_state_v2(self.state_path))

    # ------------------------------------------------------------------
    # Bookkeeping helpers (test-only; never reimplement production matching)
    # ------------------------------------------------------------------

    @property
    def model(self) -> Dict[str, LogicalConnection]:
        return self.models[self.current_isolated]

    @property
    def order(self) -> List[str]:
        return self.orders[self.current_isolated]

    def _ordered_connections(self) -> List[LogicalConnection]:
        return [self.model[lid] for lid in self.order if lid in self.model]

    def _next_logical_id(self) -> str:
        self._logical_seq += 1
        return f"L{self._logical_seq}"

    def _fresh_alias(self, suffix: str = "") -> str:
        self._alias_seq += 1
        base = f"host{self._alias_seq}"
        return f"{base}-{suffix}" if suffix else base

    def _sync_external(self, use_raw_editor: bool = False) -> None:
        text = render_flat_config(self._ordered_connections())
        root = self.roots[self.current_isolated]
        if use_raw_editor:
            revision = self.repo.get_ssh_config_text().revision
            self.repo.save_ssh_config_text(
                SaveSshConfigTextRequest(text=text, expected_revision=revision)
            )
        else:
            root.write_text(text, encoding="utf-8")
            self.repo.reload()

    def _active_identity_for_alias(self, alias: str) -> Optional[Tuple[str, str]]:
        state = read_identity_state_v2(self.state_path)
        for identity in state.identities:
            if not identity.tombstone and identity.projection.alias == alias:
                return identity.uuid, identity.display_name
        return None

    def _mutate_or_tolerate_ambiguity(self, action):
        """Run a mutation; a refusal is a valid production outcome, not a bug.

        ``ConnectionRepository`` refuses every mutation targeting an alias
        still listed in ``pending_ambiguities`` (see
        ``_assert_not_unresolved_locked``), and ``external_collide_and_rename``
        can legitimately leave a connection in that state -- its docstring
        already notes production "may resolve it as ambiguous" and that this
        harness does not predict which. Returns ``_AMBIGUOUS`` when refused,
        so the caller can skip its own model bookkeeping (nothing changed);
        otherwise returns ``action``'s result.
        """
        try:
            return action()
        except CoreError as exc:
            if exc.code is ErrorCode.MUTATION_AMBIGUOUS:
                return _AMBIGUOUS
            raise

    def _destination_is_unique(self, logical_id: str) -> bool:
        conn = self.model[logical_id]
        key = (conn.hostname, conn.port, conn.username)
        return all(
            (other.hostname, other.port, other.username) != key
            for other_id, other in self.model.items()
            if other_id != logical_id
        )

    # ------------------------------------------------------------------
    # Identity-related mutations
    # ------------------------------------------------------------------

    @rule(
        target=connections,
        hostname=hostnames(),
        port=ports(),
        username=usernames(),
        identity_files=identity_file_lists(),
        use_raw_editor=st.booleans(),
    )
    def add_connection_external(self, hostname, port, username, identity_files, use_raw_editor):
        logical_id = self._next_logical_id()
        alias = self._fresh_alias()
        conn = LogicalConnection(logical_id, (alias,), hostname, port, username, identity_files)
        self.model[logical_id] = conn
        self.order.append(logical_id)
        self._sync_external(use_raw_editor)
        self.repo.set_display_name(alias, marker_for(logical_id))
        self.ownership.bind(alias, logical_id)
        return logical_id

    @rule(target=connections, hostname=hostnames(), username=usernames())
    def add_connection_managed(self, hostname, username):
        logical_id = self._next_logical_id()
        alias = self._fresh_alias()
        payload = {
            "nickname": alias,
            "hostname": hostname,
            "protocol": "ssh",
            "display_name": marker_for(logical_id),
        }
        if username:
            payload["username"] = username
        self.repo.create_connection(payload)
        conn = LogicalConnection(logical_id, (alias,), hostname, 22, username)
        self.model[logical_id] = conn
        self.order.append(logical_id)
        self.ownership.bind(alias, logical_id)
        return logical_id

    @rule(target=connections, base=connections, username=usernames(), use_raw_editor=st.booleans())
    def add_colliding_connection(self, base, username, use_raw_editor):
        """Deliberately seed a destination collision: same anchor, new alias."""
        assume(base in self.model)
        base_conn = self.model[base]
        logical_id = self._next_logical_id()
        alias = self._fresh_alias("dup")
        conn = LogicalConnection(
            logical_id, (alias,), base_conn.hostname, base_conn.port, username
        )
        self.model[logical_id] = conn
        self.order.append(logical_id)
        self._sync_external(use_raw_editor)
        self.repo.set_display_name(alias, marker_for(logical_id))
        self.ownership.bind(alias, logical_id)
        return logical_id

    @rule(target=connections, hostname=hostnames(), username=usernames())
    def add_connection_reusing_deleted_alias(self, hostname, username):
        assume(self._graveyard)
        alias = self._graveyard.pop()
        assume(alias not in {c.alias for c in self.model.values()})
        logical_id = self._next_logical_id()
        conn = LogicalConnection(logical_id, (alias,), hostname, 22, username)
        self.model[logical_id] = conn
        self.order.append(logical_id)
        self._sync_external()
        self.repo.set_display_name(alias, marker_for(logical_id))
        self.ownership.bind(alias, logical_id)
        return logical_id

    @rule(logical_id=connections, suffix=alias_suffixes(), use_raw_editor=st.booleans())
    def managed_rename(self, logical_id, suffix, use_raw_editor):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        old_alias = conn.alias
        record = self.repo.get_record(old_alias)
        assume(record is not None)
        before = self._active_identity_for_alias(old_alias)
        new_alias = self._fresh_alias(suffix)
        payload = {
            "nickname": new_alias,
            "hostname": conn.hostname,
            "port": conn.port,
            "protocol": "ssh",
        }
        if conn.username:
            payload["username"] = conn.username
        if self._mutate_or_tolerate_ambiguity(
            lambda: self.repo.update_connection(
                old_alias, payload, expected_generation=record.generation
            )
        ) is _AMBIGUOUS:
            return
        conn.aliases = (new_alias,)
        self.ownership.bind(new_alias, logical_id)
        after = self._active_identity_for_alias(new_alias)
        if before is None or after is None or before[0] != after[0]:
            raise InvariantViolation(
                f"managed rename (destination unchanged) did not preserve UUID: "
                f"{old_alias!r} -> {new_alias!r}, before={before}, after={after}"
            )

    @rule(logical_id=connections, hostname=hostnames(), username=usernames())
    def managed_change_destination(self, logical_id, hostname, username):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        record = self.repo.get_record(conn.alias)
        assume(record is not None)
        payload = {
            "nickname": conn.alias,
            "hostname": hostname,
            "port": conn.port,
            "protocol": "ssh",
        }
        if username:
            payload["username"] = username
        if self._mutate_or_tolerate_ambiguity(
            lambda: self.repo.update_connection(
                conn.alias, payload, expected_generation=record.generation
            )
        ) is _AMBIGUOUS:
            return
        conn.hostname = hostname
        conn.username = username

    @rule(logical_id=connections, suffix=alias_suffixes(), hostname=hostnames(), username=usernames())
    def managed_rename_and_change_destination(self, logical_id, suffix, hostname, username):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        old_alias = conn.alias
        record = self.repo.get_record(old_alias)
        assume(record is not None)
        before = self._active_identity_for_alias(old_alias)
        new_alias = self._fresh_alias(suffix)
        payload = {
            "nickname": new_alias,
            "hostname": hostname,
            "port": conn.port,
            "protocol": "ssh",
        }
        if username:
            payload["username"] = username
        if self._mutate_or_tolerate_ambiguity(
            lambda: self.repo.update_connection(
                old_alias, payload, expected_generation=record.generation
            )
        ) is _AMBIGUOUS:
            return
        conn.aliases = (new_alias,)
        conn.hostname = hostname
        conn.username = username
        self.ownership.bind(new_alias, logical_id)
        after = self._active_identity_for_alias(new_alias)
        if before is None or after is None or before[0] != after[0]:
            raise InvariantViolation(
                "managed rename+destination-change (one operation) did not preserve UUID: "
                f"{old_alias!r} -> {new_alias!r}, before={before}, after={after}"
            )

    @rule(logical_id=connections)
    def managed_delete(self, logical_id):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        if self._mutate_or_tolerate_ambiguity(
            lambda: self.repo.delete_connection(conn.alias)
        ) is _AMBIGUOUS:
            return
        del self.model[logical_id]
        self.order.remove(logical_id)
        self._graveyard.append(conn.alias)

    @rule(target=connections, logical_id=connections)
    def managed_duplicate(self, logical_id):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        result = self._mutate_or_tolerate_ambiguity(
            lambda: self.repo.duplicate_connection(conn.alias)
        )
        if result is _AMBIGUOUS:
            return multiple()
        new_logical_id = self._next_logical_id()
        new_conn = LogicalConnection(
            new_logical_id, (result.id,), conn.hostname, conn.port, conn.username
        )
        self.model[new_logical_id] = new_conn
        self.order.append(new_logical_id)
        self.repo.set_display_name(result.id, marker_for(new_logical_id))
        self.ownership.bind(result.id, new_logical_id)
        return new_logical_id

    # ------------------------------------------------------------------
    # Destination mutations (external -- direct SSH config authorship)
    # ------------------------------------------------------------------

    @rule(logical_id=connections, port=ports(), use_raw_editor=st.booleans())
    def external_change_port(self, logical_id, port, use_raw_editor):
        assume(logical_id in self.model)
        self.model[logical_id].port = port
        self._sync_external(use_raw_editor)

    @rule(logical_id=connections, identity_files=identity_file_lists(), use_raw_editor=st.booleans())
    def external_change_identity_files(self, logical_id, identity_files, use_raw_editor):
        assume(logical_id in self.model)
        self.model[logical_id].identity_files = identity_files
        self._sync_external(use_raw_editor)

    @rule(logical_id=connections, suffix=alias_suffixes(), use_raw_editor=st.booleans())
    def external_rename_single(self, logical_id, suffix, use_raw_editor):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        old_alias = conn.alias
        unique = self._destination_is_unique(logical_id)
        before = self._active_identity_for_alias(old_alias)
        new_alias = self._fresh_alias(suffix)
        conn.aliases = (new_alias,)
        self._sync_external(use_raw_editor)
        self.ownership.bind(new_alias, logical_id)
        if unique and before is not None:
            after = self._active_identity_for_alias(new_alias)
            if after is None or after[0] != before[0]:
                raise InvariantViolation(
                    "external rename of the sole connection at a unique destination lost "
                    f"UUID continuity: {old_alias!r} -> {new_alias!r}, "
                    f"before={before}, after={after}"
                )

    @rule(a=connections, b=connections, use_raw_editor=st.booleans())
    def external_collide_and_rename(self, a, b, use_raw_editor):
        """Two connections swap onto one destination and rename at once.

        This is the adversarial 2-way collision from the spec. Production
        may resolve it as ambiguous, matched, or created/deleted -- this
        harness does not predict which. It only asserts (via the generic
        invariants run after every rule) that no display name marker ends
        up attached to the wrong logical connection as a result.
        """
        assume(a in self.model and b in self.model and a != b)
        conn_a, conn_b = self.model[a], self.model[b]
        conn_b.hostname = conn_a.hostname
        conn_b.port = conn_a.port
        conn_b.username = conn_a.username
        conn_b.identity_files = conn_a.identity_files
        conn_a.aliases = (self._fresh_alias("swap"),)
        conn_b.aliases = (self._fresh_alias("swap"),)
        self._sync_external(use_raw_editor)
        self.ownership.bind(conn_a.alias, a)
        self.ownership.bind(conn_b.alias, b)

    @rule(data=st.data())
    def reorder_connections(self, data):
        """Declaration order must never be identity evidence."""
        assume(len(self.order) >= 2)
        new_order = data.draw(st.permutations(self.order))
        self.orders[self.current_isolated] = list(new_order)
        self._sync_external()

    # ------------------------------------------------------------------
    # Group / metadata mutations
    # ------------------------------------------------------------------

    @rule(logical_id=connections, group_name=group_names())
    def place_in_new_group(self, logical_id, group_name):
        assume(logical_id in self.model)
        alias = self.model[logical_id].alias
        group = self.repo.create_group(group_name)
        self.repo.copy_connection_to_group(alias, group.id)

    @rule(logical_id=connections, tag=tag_names())
    def tag_connection(self, logical_id, tag):
        assume(logical_id in self.model)
        alias = self.model[logical_id].alias
        self._mutate_or_tolerate_ambiguity(
            lambda: self.repo.update_connection_metadata(alias, {"tags": [tag]})
        )

    # ------------------------------------------------------------------
    # Persistence mutations
    # ------------------------------------------------------------------

    @rule()
    def explicit_reload_idempotent(self):
        before = read_identity_state_v2(self.state_path)
        self.repo.reload()
        after = read_identity_state_v2(self.state_path)
        if after.sidecar_generation != before.sidecar_generation or after != before:
            raise InvariantViolation(
                "reload() of an unchanged SSH configuration was not a no-op: "
                f"generation {before.sidecar_generation} -> {after.sidecar_generation}"
            )

    @rule()
    def restart(self):
        del self.repo
        self.repo = ConnectionRepository(
            ssh_store=SshConfigStore(self.roots[self.current_isolated], isolated=self.current_isolated),
            state_path=self.state_path,
            legacy_config_path=self.legacy_path,
            isolated=self.current_isolated,
        )

    # ------------------------------------------------------------------
    # Mode mutations
    # ------------------------------------------------------------------

    @rule()
    def toggle_mode(self):
        new_isolated = not self.current_isolated
        if new_isolated not in self.roots:
            new_root = self.base / ("config_isolated" if new_isolated else "config_default")
            self.roots[new_isolated] = new_root
            self.models[new_isolated] = {
                lid: conn.clone() for lid, conn in self.model.items()
            }
            self.orders[new_isolated] = list(self.order)
            new_root.write_text(
                render_flat_config(
                    [self.models[new_isolated][lid] for lid in self.orders[new_isolated]]
                ),
                encoding="utf-8",
            )
        new_store = SshConfigStore(self.roots[new_isolated], isolated=new_isolated)
        self.repo.transition_ssh_config(new_store, new_isolated)
        self.current_isolated = new_isolated

    # ------------------------------------------------------------------
    # Differential OpenSSH oracle (semantic equivalence only, never identity)
    # ------------------------------------------------------------------

    @precondition(lambda self: _SSH_AVAILABLE)
    @rule(logical_id=connections)
    def check_ssh_g_matches_model(self, logical_id):
        assume(logical_id in self.model)
        conn = self.model[logical_id]
        root = self.roots[self.current_isolated]
        try:
            completed = subprocess.run(
                ["ssh", "-G", "-F", str(root), "-o", "CanonicalizeHostname=no", conn.alias],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return
        effective: Dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, _, value = line.partition(" ")
            if key in {"hostname", "port", "user"}:
                effective.setdefault(key, value)
        if effective.get("hostname") != conn.hostname:
            raise InvariantViolation(
                f"ssh -G reports hostname {effective.get('hostname')!r} for alias "
                f"{conn.alias!r}, harness model says {conn.hostname!r}"
            )
        if "port" in effective and str(conn.port) != effective["port"]:
            raise InvariantViolation(
                f"ssh -G reports port {effective['port']!r} for alias {conn.alias!r}, "
                f"harness model says {conn.port!r}"
            )

    # ------------------------------------------------------------------
    # Invariants (checked after __init__ and after every rule)
    # ------------------------------------------------------------------

    @invariant()
    def sidecar_stays_healthy(self):
        try:
            state = read_identity_state_v2(self.state_path)
        except (CoreError, ValueError, TypeError) as exc:
            raise InvariantViolation(f"sidecar state failed to load/validate: {exc}") from exc
        check_state_invariants(state, self.ownership)
        after = active_alias_state(state)
        check_exact_alias_continuity(self._last_alias_state, after)
        self._last_alias_state = after

    def teardown(self) -> None:
        self._tmpdir.cleanup()
