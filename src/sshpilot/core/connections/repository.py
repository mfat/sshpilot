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
outside its lock. The authoritative SSH configuration is always loaded first;
invalid auxiliary state degrades to an empty decoration projection without
rewriting the sidecar.
"""

from __future__ import annotations

import copy
import logging
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

from ...api.models.connection_store import (
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
    AddTagToConnectionsRequest,
    GroupSummary,
    MoveConnectionsRequest,
    thaw_safe_metadata,
    validate_safe_metadata,
)
from ...api.models.connections import (
    ConnectionHealth,
    ConnectionId,
    ConnectionSummary,
    GroupReference,
    SaveSshConfigTextRequest,
    SshConfigText,
)
from ..errors import CoreError, ErrorCode
from .models import ConnectionRecord, GroupRecord
from .service import ConnectionService
from .ssh_config_loader import LoadedSshConfiguration
from .ssh_config_store import SshConfigStore, _atomic_write_text
from .state_file import (
    ConnectionFileState,
    GroupFileState,
    read_connection_state,
    read_legacy_connection_state,
    write_connection_state,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepositoryChange:
    """Committed repository change: full before/after snapshots."""

    before: ConnectionStoreSnapshot
    after: ConnectionStoreSnapshot


@dataclass(frozen=True)
class LegacyMigrationResult:
    """Count-only diagnostics from one legacy connection-state migration."""

    groups_migrated: int = 0
    dangling_memberships_removed: int = 0
    dangling_root_entries_removed: int = 0
    dangling_metadata_entries_removed: int = 0
    dangling_parent_links_removed: int = 0


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
        *,
        expected_generation: Optional[int] = None,
    ) -> "GroupRecord": ...

    def move_connections(self, request: MoveConnectionsRequest) -> None: ...

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

    def rename_tag(self, old_tag: str, new_tag: str) -> int: ...

    def add_tag_to_connections(self, request: AddTagToConnectionsRequest) -> int: ...


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
        self._pending_changes = []
        self._service = ConnectionService(autosave=False)
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._persisted_root_order: Tuple[str, ...] = ()
        self._non_ssh_generations: Dict[str, int] = {}
        self._generation = 0
        self._migrated_legacy = False
        self._legacy_migration_result = LegacyMigrationResult()
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
            paths.update(Path(p) for p in loaded.source_paths)
            return frozenset(paths)

    @property
    def root_config_path(self) -> Path:
        """Return the daemon-selected SSH root for reload stability checks."""
        return Path(self._ssh_store.root_path)

    def reload(self) -> ConnectionStoreSnapshot:
        """Re-read authoritative sources; publish a change only when semantics differ."""
        with self._mutation_scope():
            before = self._build_snapshot_locked()
            self._load_state_locked()
            after = self._build_snapshot_locked()
            return self._notify(before, after)

    # ------------------------------------------------------------------
    # Raw SSH config text (daemon-selected editor document)
    # ------------------------------------------------------------------

    def get_ssh_config_text(self) -> SshConfigText:
        """Return the daemon-selected active SSH config text for the editor."""

        with self._lock:
            return self._ssh_store.get_text()

    @staticmethod
    def _raw_record_signature(record: ConnectionRecord) -> tuple:
        data = copy.deepcopy(record.data or {})
        for key in (
            "id",
            "nickname",
            "host",
            "aliases",
            "__host_tokens",
            "source",
        ):
            data.pop(key, None)
        return (
            record.source,
            tuple(sorted((str(key), repr(value)) for key, value in data.items())),
        )

    def _reconcile_raw_ssh_state_locked(
        self,
        previous_records: Tuple[ConnectionRecord, ...],
        ssh_config: LoadedSshConfiguration,
        file_state: ConnectionFileState,
    ) -> ConnectionFileState:
        previous = {
            record.id: record
            for record in previous_records
            if record.protocol == "ssh"
        }
        current = {
            record.id: record
            for record in ssh_config.connections
        }
        removed = set(previous) - set(current)
        added = set(current) - set(previous)
        candidates: Dict[str, List[str]] = {}
        for old_id in removed:
            signature = self._raw_record_signature(previous[old_id])
            matches = [
                new_id
                for new_id in added
                if self._raw_record_signature(current[new_id]) == signature
            ]
            candidates[old_id] = matches
        renames = {}
        for old_id, matches in candidates.items():
            if len(matches) != 1:
                continue
            new_id = matches[0]
            if sum(new_id in values for values in candidates.values()) == 1:
                renames[old_id] = new_id
        known_ids = set(current)

        def reconcile_id(connection_id: str) -> Optional[str]:
            if connection_id in renames:
                return renames[connection_id]
            if connection_id in known_ids:
                return connection_id
            return None

        groups = []
        for group in file_state.groups:
            members = []
            seen = set()
            for connection_id in group.connection_ids:
                reconciled = reconcile_id(connection_id)
                if reconciled is not None and reconciled not in seen:
                    seen.add(reconciled)
                    members.append(reconciled)
            groups.append(
                GroupFileState(
                    id=group.id,
                    name=group.name,
                    parent_id=group.parent_id,
                    order=group.order,
                    color=group.color,
                    connection_ids=tuple(members),
                )
            )

        roots = []
        seen_roots = set()
        for connection_id in file_state.root_connections:
            reconciled = reconcile_id(connection_id)
            if reconciled is not None and reconciled not in seen_roots:
                seen_roots.add(reconciled)
                roots.append(reconciled)

        metadata: Dict[str, Mapping[str, Any]] = {}
        for connection_id, values in file_state.metadata.items():
            reconciled = reconcile_id(connection_id)
            if reconciled is not None and reconciled not in metadata:
                metadata[reconciled] = values

        return ConnectionFileState(
            version=file_state.version,
            non_ssh_connections=file_state.non_ssh_connections,
            groups=tuple(groups),
            root_connections=tuple(roots),
            metadata=metadata,
        )

    def save_ssh_config_text(
        self, request: SaveSshConfigTextRequest
    ) -> SshConfigText:
        """Write raw SSH config text and reload connection state immediately.

        The write goes through the daemon-owned hardened store (revision
        check, atomic replace, backup, permissions, symlink refusal). On
        success the SSH configuration is re-read synchronously — before the
        RPC responds — so the normal connection update events fire at once
        instead of waiting for the polling watcher to notice the daemon's own
        write. A failed reload (e.g. the written document does not parse)
        rolls the file back to its previous bytes.
        """
        if type(request) is not SaveSshConfigTextRequest:
            raise TypeError(
                "request must be a SaveSshConfigTextRequest instance"
            )
        with self._mutation_scope():
            before = self._begin()
            previous_records = tuple(
                copy.deepcopy(record)
                for record in self._service.ordered_records()
                if record.protocol == "ssh"
            )
            disk_before = self._capture_transaction_files_locked(
                self._ssh_store.root_path
            )
            try:
                result = self._ssh_store.replace_text(
                    request.text,
                    request.expected_revision,
                )
                self._record_post_write_locked(
                    disk_before, self._ssh_store.root_path
                )
                ssh_config = self._ssh_store.load()
                file_state, migrated = self._read_state()
                if migrated:
                    file_state = self._reconcile_legacy_state(ssh_config, file_state)
                file_state = self._reconcile_raw_ssh_state_locked(
                    previous_records,
                    ssh_config,
                    file_state,
                )
                self._publish_state_locked(
                    ssh_config,
                    file_state,
                    migrated=migrated,
                )
                self._persist_state_file_locked()
                self._record_post_write_locked(disk_before, self._state_path)
            except Exception:
                self._rollback_after_failure_locked(disk_before)
                raise
            self._commit(before)
            return result

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
        """Load authoritative SSH state and best-effort auxiliary state."""
        ssh_config = self._ssh_store.load()
        canonical_state_present = self._state_path.exists()
        try:
            file_state, migrated = self._read_state()
            if migrated:
                file_state = self._reconcile_legacy_state(ssh_config, file_state)
            self._publish_state_locked(ssh_config, file_state, migrated=migrated)
        except Exception as exc:
            if not canonical_state_present:
                raise
            logger.warning(
                "Failed to load auxiliary connection state; continuing with "
                "SSH configuration only: %s",
                exc,
            )
            self._publish_state_locked(
                ssh_config,
                ConnectionFileState(),
                migrated=False,
            )

    def _reconcile_legacy_state(
        self,
        ssh_config: LoadedSshConfiguration,
        file_state: ConnectionFileState,
    ) -> ConnectionFileState:
        """Drop obsolete legacy references without weakening canonical state."""
        records = list(ssh_config.connections)
        for raw in file_state.non_ssh_connections:
            record = ConnectionRecord.from_dict(raw)
            if record.id and record.id not in {item.id for item in records}:
                records.append(record)
        known_ids = {record.id for record in records}
        group_ids = {group.id for group in file_state.groups}
        grouped_ids = set()
        dangling_memberships = 0
        reconciled_groups = []
        dangling_parents = 0
        for group in file_state.groups:
            members = []
            seen = set()
            for connection_id in group.connection_ids:
                if connection_id not in known_ids:
                    dangling_memberships += 1
                    continue
                if connection_id in seen:
                    continue
                seen.add(connection_id)
                members.append(connection_id)
                grouped_ids.add(connection_id)
            parent_id = group.parent_id
            if parent_id is not None and parent_id not in group_ids:
                parent_id = None
                dangling_parents += 1
            reconciled_groups.append(
                GroupFileState(
                    id=group.id,
                    name=group.name,
                    parent_id=parent_id,
                    order=group.order,
                    color=group.color,
                    connection_ids=tuple(members),
                )
            )

        roots = []
        seen_roots = set()
        dangling_roots = 0
        for connection_id in file_state.root_connections:
            if connection_id not in known_ids:
                dangling_roots += 1
                continue
            if connection_id in grouped_ids or connection_id in seen_roots:
                continue
            seen_roots.add(connection_id)
            roots.append(connection_id)
        for record in records:
            if record.id not in grouped_ids and record.id not in seen_roots:
                roots.append(record.id)
                seen_roots.add(record.id)

        metadata = {}
        dangling_metadata = 0
        for connection_id, values in file_state.metadata.items():
            if connection_id not in known_ids:
                dangling_metadata += 1
                continue
            metadata[connection_id] = values

        self._legacy_migration_result = LegacyMigrationResult(
            groups_migrated=len(reconciled_groups),
            dangling_memberships_removed=dangling_memberships,
            dangling_root_entries_removed=dangling_roots,
            dangling_metadata_entries_removed=dangling_metadata,
            dangling_parent_links_removed=dangling_parents,
        )
        logger.info(
            "Migrated legacy connection state groups=%d dangling_memberships=%d "
            "dangling_roots=%d dangling_metadata=%d dangling_parents=%d",
            len(reconciled_groups),
            dangling_memberships,
            dangling_roots,
            dangling_metadata,
            dangling_parents,
        )
        return ConnectionFileState(
            version=file_state.version,
            non_ssh_connections=file_state.non_ssh_connections,
            groups=tuple(reconciled_groups),
            root_connections=tuple(roots),
            metadata=metadata,
        )

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

        metadata: Dict[str, Dict[str, Any]] = {}
        for cid, values in file_state.metadata.items():
            # Metadata is auxiliary decoration.  Keep validated entries for
            # currently unavailable SSH aliases so a later reappearance can
            # restore the decoration without having to rewrite the sidecar.
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
        self._persisted_root_order = tuple(file_state.root_connections)
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
        groups = tuple(
            GroupSummary(
                id=group.id,
                name=group.name,
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
                connection_ids=tuple(
                    cid
                    for cid in group.connection_ids
                    if cid in connection_set
                ),
            )
            for group in service.list_groups()
        )
        grouped_ids = {
            cid
            for group in groups
            for cid in group.connection_ids
        }
        root = tuple(
            cid
            for cid in service.root_order()
            if cid in connection_set and cid not in grouped_ids
        )
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
        self._sync_persisted_root_order_locked()
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
            root_connections=self._persisted_root_order,
            metadata=thaw_safe_metadata(self._metadata),
        )

    def _sync_persisted_root_order_locked(self) -> None:
        """Apply current root membership while retaining dormant sidecar ids."""
        current_ids = {
            record.id for record in self._service.ordered_records()
        }
        current_root = [
            cid for cid in self._service.root_order() if cid in current_ids
        ]
        current_root_ids = set(current_root)
        merged: List[str] = []
        next_root = 0
        for cid in self._persisted_root_order:
            if cid in current_root_ids:
                merged.append(current_root[next_root])
                next_root += 1
            elif cid not in current_ids:
                merged.append(cid)
        merged.extend(current_root[next_root:])
        self._persisted_root_order = tuple(merged)

    # ------------------------------------------------------------------
    # Transactional CRUD
    # ------------------------------------------------------------------

    def _begin(self) -> ConnectionStoreSnapshot:
        return self._build_snapshot_locked()

    @contextmanager
    def _mutation_scope(self):
        with self._lock:
            yield
        self._dispatch_pending_changes()

    def _commit(self, before: ConnectionStoreSnapshot) -> None:
        self._notify(before, self._build_snapshot_locked())

    def _resync_from_files(self) -> None:
        """Restore in-memory state to match the persisted files after a failure."""
        ssh_config = self._ssh_store.load()
        file_state, _migrated = self._read_state()
        self._publish_state_locked(ssh_config, file_state)

    def _capture_transaction_files_locked(self, ssh_target: Optional[Path] = None):
        """Capture pre-write state for every file the mutation will touch.

        Returns a dict mapping each ``Path`` to a tuple of:
        ``(
            pre_existed: bool,
            pre_bytes: bytes,
            pre_mode: int,
            touched_by_daemon: bool,        # True only after confirmed daemon write
            post_bytes: Optional[bytes],     # daemon-written content (filled after write)
            post_identity: Optional[tuple],  # (dev, inode) after write (filled after write)
            post_mode: Optional[int],        # mode after write (filled after write)
        )``

        ``touched_by_daemon`` starts ``False`` and becomes ``True`` only after
        a confirmed daemon write.  Recording the post-write token uses
        non-following ``open``/``fstat`` checks.

        Non-SSH mutations must not capture or restore the SSH root, so
        callers must only pass ``ssh_target`` for SSH mutations.
        """
        paths = {self._state_path}
        if ssh_target is not None:
            paths.add(Path(ssh_target))
        captured = {}
        for path in paths:
            path = Path(path)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                captured[path] = (False, b"", 0, False, None, None, None)
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise CoreError(
                    ErrorCode.CONNECTION_STATE_IO_ERROR,
                    "The mutation target is unsafe",
                )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The mutation target changed during capture",
                    )
                chunks = []
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                captured[path] = (
                    True,
                    b"".join(chunks),
                    stat.S_IMODE(opened.st_mode),
                    False,    # touched_by_daemon starts False
                    None,     # post_bytes filled after write
                    None,     # post_identity filled after write
                    None,     # post_mode filled after write
                )
            finally:
                os.close(fd)
        return captured

    def _record_post_write_locked(self, captured, path: Path) -> None:
        """Record the daemon-written file identity, bytes, and mode for later verification.

        Uses non-following ``open``/``fstat`` checks.  If the daemon wrote the
        file but cannot securely record its post state, or if a regular file
        was replaced between the daemon write and post-write capture, raises
        ``MUTATION_AMBIGUOUS``.
        """
        if path not in captured:
            return
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(path), flags)
            try:
                st = os.fstat(fd)
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    # File was replaced with a symlink or non-regular file.
                    # Do not mark as daemon-owned.
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The mutation target became unsafe after daemon write",
                    )
                post_bytes = b""
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    post_bytes += chunk
                entry = captured[path]
                captured[path] = (
                    entry[0], entry[1], entry[2],
                    True,  # touched_by_daemon
                    post_bytes,
                    (st.st_dev, st.st_ino),
                    stat.S_IMODE(st.st_mode),  # post_mode
                )
            finally:
                os.close(fd)
        except CoreError:
            raise
        except OSError as exc:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "Could not record post-write state for rollback verification",
            ) from exc

    def _restore_transaction_files_locked(self, captured) -> None:
        """Restore pre-write bytes, existence, and mode.  Rejects symlinks
        and verifies the current file is still the daemon-written version
        before overwriting.

        For ``touched_by_daemon=False``: do not restore or delete the file;
        preserve any external change and resynchronize from disk.

        A rollback is successful only when original bytes, existence, and
        mode are all restored.  If mode cannot be restored, raises
        ``MUTATION_AMBIGUOUS``.
        """
        for path, (existed, data, mode, touched, post_bytes, post_identity, post_mode) in captured.items():
            if not touched:
                # The daemon never wrote this file — preserve external changes.
                # Do not restore or delete it.
                continue
            if not existed:
                # The file did not exist before the mutation, and the daemon
                # created it.  Delete only when the current file exactly
                # matches the daemon-created post-write token.
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue  # Already gone — fine.
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    raise CoreError(ErrorCode.MUTATION_AMBIGUOUS, "The mutation target is unsafe")
                if post_identity is not None and (st.st_dev, st.st_ino) != post_identity:
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The mutation target was replaced externally",
                    )
                if post_bytes is not None:
                    try:
                        current_bytes = path.read_bytes()
                    except OSError as exc:
                        raise CoreError(
                            ErrorCode.MUTATION_AMBIGUOUS,
                            "Could not verify the mutation target for rollback",
                        ) from exc
                    if current_bytes != post_bytes:
                        raise CoreError(
                            ErrorCode.MUTATION_AMBIGUOUS,
                            "The mutation target was modified externally",
                        )
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            # File pre-existed and was touched by daemon.
            # Verify the file hasn't been externally modified since the daemon wrote it.
            if post_bytes is not None and post_identity is not None:
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    # File was externally deleted — treat as ambiguity.
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The mutation target changed during rollback",
                    ) from None
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    raise CoreError(ErrorCode.MUTATION_AMBIGUOUS, "The mutation target is unsafe")
                if (st.st_dev, st.st_ino) != post_identity:
                    # File identity changed — an external edit occurred.
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The mutation target was modified externally",
                    )
                try:
                    current_bytes = path.read_bytes()
                except OSError as exc:
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "Could not verify the mutation target for rollback",
                    ) from exc
                if current_bytes != post_bytes:
                    raise CoreError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The mutation target was modified externally",
                    )
            try:
                _atomic_write_text(path, data.decode("utf-8"))
            except OSError as exc:
                raise CoreError(
                    ErrorCode.MUTATION_AMBIGUOUS,
                    "Could not restore the mutation target",
                ) from exc
            try:
                os.chmod(path, mode, follow_symlinks=False)
            except OSError as exc:
                # Mode restoration failed — resynchronize and report ambiguity.
                try:
                    self._resync_from_files()
                except Exception:
                    pass
                raise CoreError(
                    ErrorCode.MUTATION_AMBIGUOUS,
                    "Could not restore the mutation target mode",
                ) from exc

    def _rollback_after_failure_locked(self, captured, ssh_target: Optional[Path] = None) -> None:
        try:
            self._restore_transaction_files_locked(captured)
            self._resync_from_files()
        except CoreError as rollback_error:
            if rollback_error.code is ErrorCode.MUTATION_AMBIGUOUS:
                # External edit detected — resync from disk and report ambiguity.
                try:
                    self._resync_from_files()
                except Exception:
                    pass
                raise
            try:
                self._resync_from_files()
            except Exception:
                pass
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The connection mutation outcome could not be determined",
            ) from rollback_error
        except Exception as rollback_error:
            try:
                self._resync_from_files()
            except Exception:
                pass
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The connection mutation outcome could not be determined",
            ) from rollback_error

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
        with self._mutation_scope():
            before = self._begin()
            disk_before = self._capture_transaction_files_locked(self._ssh_store.root_path)
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
                    # Record the daemon-written SSH file for rollback verification.
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
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
                # Record the daemon-written state file for rollback verification.
                self._record_post_write_locked(disk_before, self._state_path)
            except Exception:
                self._rollback_after_failure_locked(disk_before)
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
        with self._mutation_scope():
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            disk_before = self._capture_transaction_files_locked(
                Path(existing.source) if existing.protocol == "ssh" and existing.source else None
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
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
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
                self._record_post_write_locked(disk_before, self._state_path)
            except Exception:
                self._rollback_after_failure_locked(disk_before)
                raise
            self._commit(before)
            return self._service.get(updated.id)

    def duplicate_connection(self, connection_id: str) -> ConnectionRecord:
        with self._mutation_scope():
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            disk_before = self._capture_transaction_files_locked(
                Path(existing.source) if existing.protocol == "ssh" and existing.source else None
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
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
                else:
                    created = self._service.duplicate(connection_id)
                    self._non_ssh_generations[created.id] = 1
                # Mirror the source's group placement on the duplicate.
                for gid in self._service.group_ids_of(connection_id):
                    self._service.copy_connection_to_group(created.id, gid)
                self._persist_state_file_locked()
                self._record_post_write_locked(disk_before, self._state_path)
            except Exception:
                self._rollback_after_failure_locked(disk_before)
                raise
            self._commit(before)
            return self._service.get(created.id)

    def delete_connection(self, connection_id: str) -> None:
        with self._mutation_scope():
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            disk_before = self._capture_transaction_files_locked(
                Path(existing.source) if existing.protocol == "ssh" and existing.source else None
            )
            try:
                if existing.protocol == "ssh":
                    result = self._ssh_store.delete(connection_id)
                    self._service.delete(connection_id)
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
                else:
                    self._service.delete(connection_id)
                    self._non_ssh_generations.pop(connection_id, None)
                self._metadata.pop(connection_id, None)
                self._persist_state_file_locked()
                self._record_post_write_locked(disk_before, self._state_path)
            except Exception:
                self._rollback_after_failure_locked(disk_before)
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
        with self._mutation_scope():
            before = self._begin()
            existing = self._service.get(connection_id)
            if existing is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            disk_before = self._capture_transaction_files_locked(
                Path(existing.source) if existing.protocol == "ssh" and existing.source else None
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
                    created = self._service.create(new_record.data)
                    result_id = created.id
                else:
                    updated = self._service.update(connection_id, new_record.data)
                    result_id = updated.id
                    self._migrate_metadata_on_rename(connection_id, result_id)
                self._overlay_ssh_generations(result.config)
                if result.touched_path:
                    self._record_post_write_locked(
                        disk_before, Path(result.touched_path),
                    )
                self._persist_state_file_locked()
                self._record_post_write_locked(disk_before, self._state_path)
            except Exception:
                self._rollback_after_failure_locked(disk_before)
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
        self._persisted_root_order = tuple(
            new_id if cid == old_id else cid
            for cid in self._persisted_root_order
        )

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    def _group_mutation(self, mutation: Callable[[], Any]):
        """Shared lock/persist/commit wrapper for group mutations.

        A failed state-file write rolls the in-memory state back to the
        persisted files so memory never diverges from disk.
        """
        with self._mutation_scope():
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
        *,
        expected_generation: Optional[int] = None,
    ) -> "GroupRecord":
        with self._mutation_scope():
            before = self._begin()
            if (
                expected_generation is not None
                and expected_generation != before.generation
            ):
                raise CoreError(
                    ErrorCode.STALE_CONNECTION_STATE,
                    "The connection store changed during the group placement",
                )
            try:
                result = self._service.place_group(group_id, parent_id, index)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return result

    def move_connections(self, request: MoveConnectionsRequest) -> None:
        with self._mutation_scope():
            before = self._begin()
            expected = request.expected_generation
            if expected is not None and expected != before.generation:
                raise CoreError(
                    ErrorCode.STALE_CONNECTION_STATE,
                    "The connection store changed during the drag operation",
                )
            try:
                result = self._service.move_connections(request)
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return result

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
        with self._mutation_scope():
            before = self._begin()
            try:
                if self._service.get(connection_id) is None:
                    raise CoreError(
                        ErrorCode.CONNECTION_NOT_FOUND,
                        "The connection does not exist",
                    )
                if not isinstance(values, Mapping):
                    raise TypeError("metadata values must be a mapping")
                current = thaw_safe_metadata(self._metadata.get(connection_id, {}))
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
            return thaw_safe_metadata(self._metadata.get(connection_id, {}))

    def add_tag_to_connections(self, request: AddTagToConnectionsRequest) -> int:
        """Add one tag to all requested connections atomically."""
        with self._mutation_scope():
            before = self._begin()
            if (
                request.expected_generation is not None
                and request.expected_generation != before.generation
            ):
                raise CoreError(
                    ErrorCode.STALE_CONNECTION_STATE,
                    "The connection store changed during the tag operation",
                )
            tag = request.tag.strip()
            requested = tuple(str(connection_id) for connection_id in request.connection_ids)
            current_values = {}
            for connection_id in requested:
                if self._service.get(connection_id) is None:
                    raise CoreError(
                        ErrorCode.CONNECTION_NOT_FOUND,
                        "The connection does not exist",
                        {"connection_id": connection_id},
                    )
                current_values[connection_id] = thaw_safe_metadata(
                    self._metadata.get(connection_id, {})
                )
            updated_values = {}
            changed_count = 0
            for connection_id, values in current_values.items():
                tags = values.get("tags")
                normalized = [str(item).strip() for item in (tags or []) if str(item).strip()]
                if not any(item.casefold() == tag.casefold() for item in normalized):
                    normalized.append(tag)
                    changed_count += 1
                values["tags"] = normalized
                updated_values[connection_id] = values
            if changed_count == 0:
                return 0
            try:
                self._metadata.update(
                    {
                        connection_id: validate_safe_metadata(values)
                        for connection_id, values in updated_values.items()
                    }
                )
                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
            return changed_count

    def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """Rename a tag across every connection (case-insensitive, deduped)."""
        if not isinstance(old_tag, str) or not old_tag.strip():
            raise ValueError("old tag must be a non-empty string")
        if not isinstance(new_tag, str) or not new_tag.strip():
            raise ValueError("new tag must be a non-empty string")
        with self._mutation_scope():
            before = self._begin()
            old_lower = old_tag.lower()
            changed = False
            renamed = 0
            try:
                updated_metadata = {}
                for connection_id, stored_values in self._metadata.items():
                    values = thaw_safe_metadata(stored_values)
                    tags = values.get("tags")
                    if not isinstance(tags, list):
                        updated_metadata[connection_id] = values
                        continue
                    new_tags: List[str] = []
                    seen = set()
                    for tag in tags:
                        tag_str = str(tag)
                        if tag_str.lower() == old_lower:
                            tag_str = new_tag
                            renamed += 1
                        lowered = tag_str.lower()
                        if lowered not in seen:
                            seen.add(lowered)
                            new_tags.append(tag_str)
                    if new_tags != tags:
                        values["tags"] = new_tags
                        changed = True
                    updated_metadata[connection_id] = values
                if changed:
                    self._metadata = {
                        connection_id: validate_safe_metadata(values)
                        for connection_id, values in updated_metadata.items()
                    }
                if changed:
                    self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            if changed:
                self._commit(before)
            return renamed

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
            self._pending_changes.append(
                (RepositoryChange(before=before, after=after), listeners)
            )
        return after

    def _dispatch_pending_changes(self) -> None:
        with self._lock:
            pending = list(self._pending_changes)
            self._pending_changes.clear()
        for change, listeners in pending:
            for callback in listeners:
                callback(change)

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
