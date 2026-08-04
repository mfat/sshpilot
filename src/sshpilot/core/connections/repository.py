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
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

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


@runtime_checkable
class ConnectionRepositoryProtocol(Protocol):
    """The minimal repository surface consumed by the application service.

    Keep this protocol limited to operations the service actually needs; the
    service never inspects repository private fields.
    """

    def snapshot(self) -> ConnectionStoreSnapshot: ...

    def list_records(self) -> Tuple[ConnectionRecord, ...]: ...

    def get_record(self, connection_id: str) -> Optional[ConnectionRecord]: ...

    def get_editor_record(self, connection_id: str) -> Optional[ConnectionRecord]: ...

    def discover_paths(self) -> frozenset: ...

    def reload(self) -> ConnectionStoreSnapshot: ...

    def add_listener(self, callback: ChangeListener) -> None: ...

    def remove_listener(self, callback: ChangeListener) -> None: ...

    def create_connection(self, data: Mapping[str, Any]) -> ConnectionRecord: ...

    def update_connection(
        self,
        connection_id: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord: ...

    def duplicate_connection(self, connection_id: str) -> ConnectionRecord: ...

    def delete_connection(self, connection_id: str) -> None: ...

    def split_connection(
        self,
        connection_id: str,
        original_host_token: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord: ...

    def create_group(
        self,
        name: str,
        *,
        parent_id: Optional[str] = None,
        color: str = "",
    ) -> "GroupRecord": ...

    def rename_group(self, group_id: str, new_name: str) -> "GroupRecord": ...

    def delete_group(self, group_id: str) -> None: ...

    def set_group_color(self, group_id: str, color: str) -> "GroupRecord": ...

    def place_group(
        self,
        group_id: str,
        parent_id: Optional[str],
        index: int,
    ) -> "GroupRecord": ...

    def assign_connection_to_group(
        self, connection_id: str, group_id: Optional[str]
    ) -> ConnectionRecord: ...

    def copy_connection_to_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord: ...

    def remove_connection_from_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord: ...

    def reorder_connection(
        self,
        connection_id: str,
        target_connection_id: str,
        group_id: Optional[str],
        position: str,
    ) -> None: ...

    def update_connection_metadata(
        self, connection_id: str, values: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def rename_tag(self, old_tag: str, new_tag: str) -> None: ...


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
        self._publish_state_locked(ssh_config, file_state, migrated=migrated)

    def _publish_state_locked(
        self,
        ssh_config: LoadedSshConfiguration,
        file_state: ConnectionFileState,
        *,
        migrated: bool = False,
    ) -> None:
        """Swap in validated in-memory state for the given sources.

        Cross-validation runs through the snapshot constructor before any
        field is reassigned, so a bad source set never publishes partial state.
        """
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
        ssh_generations = {
            record.id: record.generation for record in ssh_config.connections
        }
        for record in service.ordered_records():
            if record.protocol != "ssh":
                non_ssh_generations[record.id] = self._non_ssh_generations.get(
                    record.id, 0
                )
            else:
                record.generation = ssh_generations.get(record.id, 0)

        ConnectionRepository._assemble(service, metadata, self._generation)

        self._service = service
        self._metadata = metadata
        self._non_ssh_generations = non_ssh_generations
        self._migrated_legacy = migrated or self._migrated_legacy

    def _overlay_ssh_generations(self, config: LoadedSshConfiguration) -> None:
        """Copy the store's per-connection generations onto service records."""
        generations = {
            record.id: record.generation for record in config.connections
        }
        for record in self._service.ordered_records():
            if record.protocol == "ssh" and record.id in generations:
                record.generation = generations[record.id]

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
    # Transactional CRUD
    # ------------------------------------------------------------------

    def _begin(self) -> ConnectionStoreSnapshot:
        return self._build_snapshot_locked()

    def _commit(self, before: ConnectionStoreSnapshot) -> None:
        self._notify(before, self._build_snapshot_locked())

    def _resync_from_files(self) -> None:
        """Restore in-memory state to match the persisted files after a failure."""
        ssh_config = self._ssh_store.load()
        file_state, _migrated = self._read_state()
        self._publish_state_locked(ssh_config, file_state)

    def _validate_new_nickname(self, nickname: str) -> str:
        nickname = (nickname or "").strip()
        if not nickname:
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                "A nickname is required",
            )
        existing = {c.id for c in self._service.ordered_records()}
        if nickname in existing:
            raise CoreError(
                ErrorCode.CONNECTION_ALREADY_EXISTS,
                "A connection with this name already exists",
            )
        return nickname

    def create_connection(self, data: Mapping[str, Any]) -> ConnectionRecord:
        with self._lock:
            before = self._begin()
            payload = dict(data)
            payload.pop("uuid", None)
            protocol = str(payload.get("protocol") or "ssh").strip() or "ssh"
            try:
                if protocol == "ssh":
                    result = self._ssh_store.create(payload)
                    fresh = next(
                        r for r in result.config.connections
                        if r.id == result.connection_id
                    )
                    created = self._service.create(fresh.data)
                    self._overlay_ssh_generations(result.config)
                else:
                    nickname = self._validate_new_nickname(
                        str(payload.get("nickname") or "")
                    )
                    payload["id"] = nickname
                    payload["nickname"] = nickname
                    created = self._service.create(payload)
                    self._non_ssh_generations[created.id] = 1
                # Root order / membership / metadata always live in
                # connections.json; persist it after every committed mutation.
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return self._service.get(created.id)

    def update_connection(
        self,
        connection_id: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        with self._lock:
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            payload = dict(data)
            payload.pop("uuid", None)
            protocol = str(
                payload.get("protocol") or existing.protocol or "ssh"
            ).strip() or "ssh"
            new_nick = str(payload.get("nickname") or "").strip()
            if new_nick and new_nick != connection_id:
                self._validate_new_nickname(new_nick)
            try:
                if protocol == "ssh":
                    result = self._ssh_store.update(
                        connection_id,
                        payload,
                        expected_generation=expected_generation,
                    )
                    new_id = result.connection_id
                    fresh = next(
                        r for r in result.config.connections
                        if r.id == new_id
                    )
                    updated = self._service.update(connection_id, fresh.data)
                    self._overlay_ssh_generations(result.config)
                else:
                    if (
                        expected_generation is not None
                        and expected_generation
                        != self._non_ssh_generations.get(connection_id, 0)
                    ):
                        raise CoreError(
                            ErrorCode.STALE_CONNECTION_STATE,
                            "The connection has been modified since it was last read",
                        )
                    nickname = new_nick or connection_id
                    payload["id"] = nickname
                    payload["nickname"] = nickname
                    updated = self._service.update(connection_id, payload)
                    if new_nick and new_nick != connection_id:
                        gen = self._non_ssh_generations.pop(connection_id, 0)
                        self._non_ssh_generations[new_nick] = gen + 1
                    else:
                        self._non_ssh_generations[connection_id] = (
                            self._non_ssh_generations.get(connection_id, 0) + 1
                        )
                self._migrate_metadata_on_rename(connection_id, updated.id)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return self._service.get(updated.id)

    def duplicate_connection(self, connection_id: str) -> ConnectionRecord:
        with self._lock:
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            try:
                if existing.protocol == "ssh":
                    result = self._ssh_store.duplicate(connection_id)
                    fresh = next(
                        r for r in result.config.connections
                        if r.id == result.connection_id
                    )
                    created = self._service.create(fresh.data)
                    self._overlay_ssh_generations(result.config)
                else:
                    created = self._service.duplicate(connection_id)
                    self._non_ssh_generations[created.id] = 1
                # Mirror the source's group placement on the duplicate.
                for gid in self._service.group_ids_of(connection_id):
                    self._service.copy_connection_to_group(created.id, gid)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return self._service.get(created.id)

    def delete_connection(self, connection_id: str) -> None:
        with self._lock:
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            try:
                if existing.protocol == "ssh":
                    self._ssh_store.delete(connection_id)
                    self._service.delete(connection_id)
                else:
                    self._service.delete(connection_id)
                    self._non_ssh_generations.pop(connection_id, None)
                self._metadata.pop(connection_id, None)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)

    def split_connection(
        self,
        connection_id: str,
        original_host_token: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        with self._lock:
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            try:
                result = self._ssh_store.split(
                    connection_id,
                    original_host_token,
                    data,
                    expected_generation=expected_generation,
                )
                fresh_ids = {r.id for r in result.config.connections}
                new_record = next(
                    r for r in result.config.connections
                    if r.id == result.connection_id
                )
                if connection_id in fresh_ids:
                    # A different alias was split out: the original survives
                    # and the new standalone connection is created (its
                    # metadata starts fresh).
                    created = self._service.create(new_record.data)
                    result_id = created.id
                else:
                    # The split removed the original token: rename the old
                    # record into the new standalone connection and carry its
                    # metadata over.
                    updated = self._service.update(connection_id, new_record.data)
                    result_id = updated.id
                    self._migrate_metadata_on_rename(connection_id, result_id)
                self._overlay_ssh_generations(result.config)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return self._service.get(result_id)

    def _persist_state_file_locked(self) -> None:
        write_connection_state(self._state_path, self._build_file_state_locked())

    def _migrate_metadata_on_rename(self, old_id: str, new_id: str) -> None:
        """Move metadata to the new id (in-memory; the caller persists once)."""
        if old_id == new_id:
            return
        if old_id in self._metadata:
            self._metadata[new_id] = self._metadata.pop(old_id)

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    def _group_mutation(self, mutation: Callable[[], Any]):
        """Shared lock/persist/commit wrapper for group mutations.

        A failed state-file write rolls the in-memory state back to the
        persisted files so memory never diverges from disk.
        """
        with self._lock:
            before = self._begin()
            try:
                result = mutation()
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return result

    def create_group(
        self,
        name: str,
        *,
        parent_id: Optional[str] = None,
        color: str = "",
    ) -> "GroupRecord":
        return self._group_mutation(
            lambda: self._service.create_group(
                name, parent_id=parent_id, color=color
            )
        )

    def rename_group(self, group_id: str, new_name: str) -> "GroupRecord":
        return self._group_mutation(
            lambda: self._service.rename_group(group_id, new_name)
        )

    def delete_group(self, group_id: str) -> None:
        self._group_mutation(lambda: self._service.delete_group(group_id))

    def set_group_color(self, group_id: str, color: str) -> "GroupRecord":
        return self._group_mutation(
            lambda: self._service.set_group_color(group_id, color)
        )

    def place_group(
        self,
        group_id: str,
        parent_id: Optional[str],
        index: int,
    ) -> "GroupRecord":
        return self._group_mutation(
            lambda: self._service.place_group(group_id, parent_id, index)
        )

    def assign_connection_to_group(
        self, connection_id: str, group_id: Optional[str]
    ) -> ConnectionRecord:
        return self._group_mutation(
            lambda: self._service.assign_group(connection_id, group_id)
        )

    def copy_connection_to_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        return self._group_mutation(
            lambda: self._service.copy_connection_to_group(connection_id, group_id)
        )

    def remove_connection_from_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        return self._group_mutation(
            lambda: self._service.remove_connection_from_group(
                connection_id, group_id
            )
        )

    def reorder_connection(
        self,
        connection_id: str,
        target_connection_id: str,
        group_id: Optional[str],
        position: str,
    ) -> None:
        self._group_mutation(
            lambda: self._service.reorder_connection(
                connection_id, target_connection_id, group_id, position
            )
        )

    # ------------------------------------------------------------------
    # Metadata operations
    # ------------------------------------------------------------------

    def update_connection_metadata(
        self, connection_id: str, values: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Merge safe metadata; ``None`` values remove the corresponding key."""
        with self._lock:
            before = self._begin()
            try:
                if self._service.get(connection_id) is None:
                    raise CoreError(
                        ErrorCode.CONNECTION_NOT_FOUND,
                        "The connection does not exist",
                    )
                if not isinstance(values, Mapping):
                    raise TypeError("metadata values must be a mapping")
                current = dict(self._metadata.get(connection_id, {}))
                for key, value in values.items():
                    if value is None:
                        current.pop(key, None)
                    else:
                        current[key] = value
                if current:
                    self._metadata[connection_id] = validate_safe_metadata(current)
                else:
                    self._metadata.pop(connection_id, None)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return copy.deepcopy(self._metadata.get(connection_id, {}))

    def rename_tag(self, old_tag: str, new_tag: str) -> None:
        """Rename a tag across every connection (case-insensitive, deduped)."""
        if not isinstance(old_tag, str) or not old_tag.strip():
            raise ValueError("old tag must be a non-empty string")
        if not isinstance(new_tag, str) or not new_tag.strip():
            raise ValueError("new tag must be a non-empty string")
        with self._lock:
            before = self._begin()
            old_lower = old_tag.lower()
            changed = False
            try:
                for values in self._metadata.values():
                    tags = values.get("tags")
                    if not isinstance(tags, list):
                        continue
                    new_tags: List[str] = []
                    seen = set()
                    for tag in tags:
                        tag_str = str(tag)
                        if tag_str.lower() == old_lower:
                            tag_str = new_tag
                        lowered = tag_str.lower()
                        if lowered not in seen:
                            seen.add(lowered)
                            new_tags.append(tag_str)
                    if new_tags != tags:
                        values["tags"] = new_tags
                        changed = True
                if changed:
                    self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            if changed:
                self._commit(before)

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
