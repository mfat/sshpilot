"""Headless connection repository (GTK-free, daemon-owned).

``ConnectionRepository`` is the daemon's single owner of saved connection
state. It composes:

* :class:`~sshpilot.core.connections.SshConfigStore` — the daemon-owned SSH
  config read/mutation path (lossless, atomic, revision-checked);
* :class:`~sshpilot.core.connections.ConnectionService` — the in-memory
  connection/group domain state (multi-group membership);
* the dedicated ``connections.json`` state storage (non-SSH connections,
  groups/ordering, safe metadata) with one-time read-only legacy migration
  from ``config.json``.

The repository publishes immutable :class:`ConnectionStoreSnapshot` objects
and fires ``RepositoryChange`` callbacks after every committed change,
outside its lock. No partial state ever becomes visible: the initial load and
every reload build a fully validated candidate before publishing.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ...api.models.connection_store import (
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
    GroupSummary,
    validate_safe_metadata,
)
from ...api.models.connections import (
    ConnectionHealth,
    ConnectionId,
    ConnectionSummary,
    GroupReference,
)
from ..errors import CoreError, ErrorCode
from .models import ConnectionRecord, GroupRecord
from .service import ConnectionService
from .ssh_config_loader import LoadedSshConfiguration
from .ssh_config_store import SshConfigStore
from .state_file import (
    ConnectionFileState,
    GroupFileState,
    read_connection_state,
    read_legacy_connection_state,
    write_connection_state,
)


@dataclass(frozen=True)
class RepositoryChange:
    """Committed repository change: full before/after snapshots."""

    before: ConnectionStoreSnapshot
    after: ConnectionStoreSnapshot


ChangeListener = Callable[[RepositoryChange], None]


def _repository_error(message: str) -> CoreError:
    return CoreError(ErrorCode.CONNECTION_STATE_IO_ERROR, message)


class ConnectionRepository:
    """Daemon-owned connection store: SSH config + connections.json."""

    def __init__(
        self,
        *,
        ssh_store: SshConfigStore,
        state_path: Path,
        legacy_config_path: Path,
        isolated: bool,
    ) -> None:
        self._lock = threading.RLock()
        self._ssh_store = ssh_store
        self._state_path = Path(state_path)
        self._legacy_config_path = Path(legacy_config_path)
        self._isolated = bool(isolated)
        self._listeners: List[ChangeListener] = []
        self._service = ConnectionService(autosave=False)
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._non_ssh_generations: Dict[str, int] = {}
        self._generation = 0
        self._migrated_legacy = False
        self._initial_load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> ConnectionStoreSnapshot:
        with self._lock:
            return self._build_snapshot_locked()

    def list_records(self) -> Tuple[ConnectionRecord, ...]:
        with self._lock:
            return tuple(copy.deepcopy(r) for r in self._service.ordered_records())

    def get_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        with self._lock:
            record = self._service.get(connection_id)
            return record

    def get_editor_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        with self._lock:
            record = self._service.get(connection_id)
            if record is None:
                return None
            record.generation = self._generation_for_locked(connection_id)
            return record

    def discover_paths(self) -> frozenset:
        with self._lock:
            paths = {self._state_path}
            loaded = self._ssh_store.load()
            paths.update(str(p) for p in loaded.source_paths)
            return frozenset(paths)

    def reload(self) -> ConnectionStoreSnapshot:
        """Re-read authoritative sources; publish a change only when semantics differ."""
        with self._lock:
            before = self._build_snapshot_locked()
            self._load_state_locked()
            after = self._build_snapshot_locked()
        return self._notify(before, after)

    def add_listener(self, callback: ChangeListener) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: ChangeListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Initial load (no partial state may become visible)
    # ------------------------------------------------------------------

    def _initial_load(self) -> None:
        with self._lock:
            self._load_state_locked()
            # Persist a one-time read-only migration after the load validated.
            if self._migrated_legacy:
                write_connection_state(self._state_path, self._build_file_state_locked())

    def _read_state(self) -> Tuple[ConnectionFileState, bool]:
        """Read the dedicated file, or the legacy values when it is absent."""
        if self._state_path.exists():
            return read_connection_state(self._state_path), False
        return read_legacy_connection_state(self._legacy_config_path), True

    def _load_state_locked(self) -> None:
        """Load SSH config + connections.json into validated in-memory state.

        Raises (leaving the previous state untouched) when any participating
        source is unreadable, malformed, or inconsistent. A missing dedicated
        file migrates defaults or legacy values.
        """
        ssh_config = self._ssh_store.load()
        file_state, migrated = self._read_state()

        connections = list(ssh_config.connections)
        for raw in file_state.non_ssh_connections:
            record = ConnectionRecord.from_dict(raw)
            if record.id and record.id not in {c.id for c in connections}:
                connections.append(record)

        groups_blob: Dict[str, Any] = {
            "groups": {
                group.id: group.to_dict() for group in file_state.groups
            },
            "connections": {},
            "root_connections": list(file_state.root_connections),
        }
        service = ConnectionService(autosave=False)
        service.replace_all(
            [record.data for record in connections],
            groups=groups_blob,
        )

        existing_ids = {record.id for record in connections}
        metadata: Dict[str, Dict[str, Any]] = {}
        for cid, values in file_state.metadata.items():
            if cid in existing_ids:
                metadata[cid] = validate_safe_metadata(values)

        non_ssh_generations: Dict[str, int] = {}
        for record in connections:
            if record.protocol != "ssh":
                non_ssh_generations[record.id] = record.generation or 0

        # Cross-validation: the snapshot constructor enforces referential
        # integrity (unknown members, orphan parents, cycles, root rules).
        candidate = ConnectionRepository._assemble(
            service,
            metadata,
            self._generation,
        )

        self._service = service
        self._metadata = metadata
        self._non_ssh_generations = non_ssh_generations
        self._migrated_legacy = migrated
        candidate  # validated above; published implicitly by assignment

    def _generation_for_locked(self, connection_id: str) -> int:
        record = self._service.get(connection_id)
        if record is None:
            return 0
        if record.protocol != "ssh":
            return self._non_ssh_generations.get(connection_id, 0)
        return record.generation

    # ------------------------------------------------------------------
    # Snapshot assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _assemble(
        service: ConnectionService,
        metadata: Mapping[str, Mapping[str, Any]],
        generation: int,
    ) -> ConnectionStoreSnapshot:
        records = service.ordered_records()
        connection_set = {record.id for record in records}
        group_names = {g.id: g.name for g in service.list_groups()}

        def _summary(record: ConnectionRecord) -> ConnectionSummary:
            port = int(record.port) if 1 <= int(record.port or 22) <= 65535 else 22
            groups = tuple(
                GroupReference(id=gid, name=group_names.get(gid, ""))
                for gid in service.group_ids_of(record.id)
            )
            return ConnectionSummary(
                id=ConnectionId(record.id),
                nickname=record.nickname,
                host=record.host or str(record.data.get("host") or record.id),
                hostname=record.hostname,
                username=record.username,
                port=port,
                protocol=record.protocol or "ssh",
                health=ConnectionHealth.UNKNOWN,
                groups=groups,
            )

        connections = tuple(_summary(r) for r in records)
        # No filtering: referential integrity is enforced strictly by the
        # snapshot constructor, so a dangling membership or root id fails the
        # whole load instead of being silently discarded.
        groups = tuple(
            GroupSummary(
                id=group.id,
                name=group.name,
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
                connection_ids=tuple(group.connection_ids),
            )
            for group in service.list_groups()
        )
        root = tuple(service.root_order())
        metadata_summaries = tuple(
            ConnectionMetadataSummary(
                connection_id=ConnectionId(cid),
                values=values,
            )
            for cid, values in metadata.items()
            if cid in connection_set
        )
        return ConnectionStoreSnapshot(
            generation=generation,
            connections=connections,
            groups=groups,
            root_connection_ids=root,
            metadata=metadata_summaries,
        )

    def _build_snapshot_locked(self) -> ConnectionStoreSnapshot:
        return self._assemble(
            self._service, self._metadata, self._generation
        )

    def _build_file_state_locked(self) -> ConnectionFileState:
        groups = tuple(
            GroupFileState(
                id=g.id,
                name=g.name,
                parent_id=g.parent_id,
                order=g.order,
                color=g.color,
                connection_ids=tuple(g.connection_ids),
            )
            for g in self._service.list_groups()
        )
        non_ssh = tuple(
            copy.deepcopy(record.data)
            for record in self._service.ordered_records()
            if record.protocol != "ssh"
        )
        return ConnectionFileState(
            version=1,
            non_ssh_connections=non_ssh,
            groups=groups,
            root_connections=tuple(self._service.root_order()),
            metadata=copy.deepcopy(self._metadata),
        )

    # ------------------------------------------------------------------
    # Event dispatch (outside the lock)
    # ------------------------------------------------------------------

    def _notify(self, before: ConnectionStoreSnapshot, after: ConnectionStoreSnapshot) -> ConnectionStoreSnapshot:
        if self._semantically_equal(before, after):
            return after
        with self._lock:
            self._generation += 1
            after = self._build_snapshot_locked()
            listeners = list(self._listeners)
        change = RepositoryChange(before=before, after=after)
        for callback in listeners:
            callback(change)
        return after

    @staticmethod
    def _semantically_equal(
        before: ConnectionStoreSnapshot, after: ConnectionStoreSnapshot
    ) -> bool:
        return (
            before.connections == after.connections
            and before.groups == after.groups
            and before.root_connection_ids == after.root_connection_ids
            and before.metadata == after.metadata
        )
