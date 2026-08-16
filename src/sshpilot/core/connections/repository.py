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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

from ...api.models.connection_store import (
    ConnectionMetadataSummary,
    ConnectionPlacementMode,
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
    MAX_DISPLAY_NAME_LENGTH,
)
from ...api.models.common import validate_ssh_host_alias
from ..errors import CoreError, ErrorCode
from .models import ConnectionRecord, GroupRecord
from .identity_repository_adapter import (
    projections_from_records,
    reconcile_identity_state,
    state_to_service_file_state,
)
from .identity_state_v2 import (
    ConnectionReference,
    IdentityStateV2,
    ReferenceKind,
    UuidGroupState,
    migrate_v1_state,
    new_uuid4,
)
from .service import ConnectionService
from .ssh_config_loader import LoadedSshConfiguration
from .ssh_config_store import SshConfigStore, _atomic_write_text
from .state_file import (
    ConnectionStateFileKind,
    ConnectionFileState,
    GroupFileState,
    probe_connection_state_file,
    read_identity_state_v2,
    recover_pending_identity_transaction,
    read_connection_state,
    read_legacy_connection_state,
    write_identity_state_v2,
    identity_transaction_intent_path,
    write_pending_identity_transaction,
    clear_pending_identity_transaction,
)
from .identity_state_v2 import IdentityRecoveryAction, IdentityTransactionIntent

# Kept as a module attribute for legacy test/integration hooks.  Production
# writes in this repository are v2-only after migration.
from .state_file import write_connection_state as write_connection_state

logger = logging.getLogger(__name__)


def _display_name_for_record(
    record: ConnectionRecord, identity: Any = None
) -> str:
    """Return the safe public label for an SSH or protocol-local record."""
    if identity is not None:
        return identity.display_name
    value = record.data.get("display_name") if isinstance(record.data, Mapping) else None
    if type(value) is str:
        normalized = value.strip()
        if normalized and len(normalized) <= MAX_DISPLAY_NAME_LENGTH:
            return normalized
    return record.nickname


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


# Schema version for the portable ``connection_store`` backup section built by
# ``snapshot_for_backup``/consumed by ``restore_connection_store``. Bump only
# on a breaking shape change; ``restore_connection_store`` skips (with a
# warning) any section whose version it does not understand.
CONNECTION_STORE_SECTION_VERSION = 1

# Portable, non-secret launch configuration for built-in non-SSH protocols,
# beyond the generic id/nickname/hostname/username/port/display_name fields
# already captured above. Declared here (core) rather than discovered from
# ``sshpilot.plugins`` at runtime: the dependency-direction boundary
# (``tests/core/test_dependency_boundary.py``) forbids ``core`` from importing
# the plugin package, so this list is a deliberately duplicated, centralized
# allowlist for the built-in protocols only — it must be kept in sync by hand
# with each plugin's own ``connection_fields()`` declaration
# (``src/sshpilot/plugins/builtin/*/__init__.py``). A third-party/dynamically
# loaded protocol gets no portable-field backup support until it's added here;
# its connections still round-trip with the generic fields only.
_PORTABLE_PROTOCOL_FIELDS: Dict[str, Tuple[str, ...]] = {
    "serial": ("device", "baud", "flow", "databits", "parity", "stopbits"),
    "docker": ("container", "command", "runtime", "docker_host", "user", "workdir"),
    "k8s": ("pod", "container", "namespace", "kube_context", "kubeconfig", "command"),
    "mosh": ("keyfile", "extra_ssh_opts", "predict", "mosh_port"),
}

# Defense-in-depth secret filter mirroring the GTK client's own
# ``_SENSITIVE_PARTS`` convention (``sshpilot/gtk/daemon_connection_services.py``):
# no field in ``_PORTABLE_PROTOCOL_FIELDS`` currently matches this today, but
# a hand-crafted backup file or a future plugin field must never be able to
# smuggle a secret into the logical connection_store through ``plugin_data``.
_SENSITIVE_DATA_PARTS = ("password", "passphrase", "secret", "token", "credential", "private_key")


def _is_sensitive_field_name(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_DATA_PARTS)


def _portable_plugin_data(protocol: str, data: Mapping[str, Any]) -> Dict[str, Any]:
    """The subset of ``record.data`` that is safe, portable, protocol-declared
    launch configuration — never arbitrary/unknown keys, never anything
    matching a secret-like field name."""
    allowed = _PORTABLE_PROTOCOL_FIELDS.get(protocol, ())
    return {
        key: data[key]
        for key in allowed
        if key in data and not _is_sensitive_field_name(key)
    }


@dataclass(frozen=True)
class ConnectionStoreRestoreResult:
    """Non-fatal diagnostics from one ``restore_connection_store`` call."""

    warnings: Tuple[str, ...] = ()


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

    def transition_ssh_config(self, ssh_store: SshConfigStore, isolated: bool) -> ConnectionStoreSnapshot: ...

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

    def set_display_name(
        self,
        connection_id: str,
        display_name: str,
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord: ...

    def ssh_alias_for(self, connection_id: str) -> str: ...

    def rename_tag(self, old_tag: str, new_tag: str) -> int: ...

    def add_tag_to_connections(self, request: AddTagToConnectionsRequest) -> int: ...

    def snapshot_for_backup(self) -> Dict[str, Any]: ...

    def restore_connection_store(
        self, section: Mapping[str, Any], *, mode: str = "merge"
    ) -> "ConnectionStoreRestoreResult": ...


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
        self._identity_state: Optional[IdentityStateV2] = None
        self._identity_state_unavailable = False
        self._identity_recovery_required = False
        self._pending_identity_target: Optional[IdentityStateV2] = None
        self._loaded_ssh_config: Optional[LoadedSshConfiguration] = None
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
            record = self._service.get(self._resolve_internal_id_locked(connection_id))
            return record

    def get_editor_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        with self._lock:
            record = self._service.get(self._resolve_internal_id_locked(connection_id))
            if record is None:
                return None
            record.generation = self._generation_for_locked(connection_id)
            return record

    def discover_paths(self) -> frozenset:
        with self._lock:
            paths = {self._state_path}
            loaded = self._ssh_store.load()
            paths.update(Path(p) for p in (loaded.watch_paths or loaded.source_paths))
            return frozenset(paths)

    @property
    def root_config_path(self) -> Path:
        """Return the daemon-selected SSH root for reload stability checks."""
        return Path(self._ssh_store.root_path)

    @property
    def ssh_config_isolated(self) -> bool:
        """Whether this daemon repository uses its isolated SSH root."""
        return self._ssh_store.isolated

    def reload(self) -> ConnectionStoreSnapshot:
        """Re-read authoritative sources; publish a change only when semantics differ."""
        with self._mutation_scope():
            before = self._build_snapshot_locked()
            self._load_state_locked()
            after = self._build_snapshot_locked()
            return self._notify(before, after)

    def transition_ssh_config(
        self, ssh_store: SshConfigStore, isolated: bool
    ) -> ConnectionStoreSnapshot:
        """Atomically replace the active daemon SSH config store and reload."""
        if not isinstance(ssh_store, SshConfigStore):
            raise TypeError("an SshConfigStore is required")
        with self._mutation_scope():
            before = self._build_snapshot_locked()
            old_store = self._ssh_store
            old_isolated = self._isolated
            target_isolated = bool(isolated)
            mode_changed = old_isolated != target_isolated
            try:
                # Load before publication so malformed or inaccessible target
                # configuration cannot leave a partially switched authority.
                ssh_store.load()
                self._ssh_store = ssh_store
                self._isolated = target_isolated
                self._load_state_locked(
                    allow_tombstone_resurrection=mode_changed,
                )
            except Exception:
                self._ssh_store = old_store
                self._isolated = old_isolated
                self._load_state_locked(
                    allow_tombstone_resurrection=mode_changed,
                )
                raise
            after = self._build_snapshot_locked()
            return self._notify(before, after)

    # ------------------------------------------------------------------
    # Raw SSH config text (daemon-selected editor document)
    # ------------------------------------------------------------------

    def get_ssh_config_text(self) -> SshConfigText:
        """Return the daemon-selected active SSH config text for the editor."""

        with self._lock:
            return self._ssh_store.get_text()

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
            disk_before = self._capture_transaction_files_locked(
                self._ssh_store.root_path
            )
            try:
                prepared = self._ssh_store.prepare_replace_text(
                    request.text, request.expected_revision
                )
                if not self._identity_state_unavailable:
                    intent = self._prepare_identity_intent_locked(
                        prepared, operation_label="raw_config_edit"
                    )
                self._ssh_store.commit_prepared(prepared)
                result = self._ssh_store.get_text()
                self._record_post_write_locked(
                    disk_before, self._ssh_store.root_path
                )
                ssh_config = self._ssh_store.load()
                if not self._identity_state_unavailable:
                    self._commit_identity_target_locked(
                        intent, ssh_config, disk_before
                    )
                    self._finish_identity_intent_locked()
                else:
                    # A corrupt/unsupported sidecar must not be replaced by
                    # an invented registry merely because SSH text is valid.
                    self._publish_state_locked(
                        ssh_config, ConnectionFileState(), migrated=False
                    )
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

    def _read_state(self) -> Tuple[ConnectionFileState, bool]:
        """Read the dedicated file, or the legacy values when it is absent."""
        if self._state_path.exists():
            return read_connection_state(self._state_path), False
        return read_legacy_connection_state(self._legacy_config_path), True

    def _load_state_locked(
        self,
        *,
        allow_tombstone_resurrection: bool = False,
    ) -> None:
        """Load SSH first, then the UUID sidecar or a one-time v1 migration."""
        ssh_config = self._ssh_store.load()
        self._loaded_ssh_config = ssh_config
        projections = projections_from_records(ssh_config.connections)
        try:
            kind = probe_connection_state_file(self._state_path)
            if kind is ConnectionStateFileKind.V2:
                intent_path = identity_transaction_intent_path(self._state_path)
                # ``Path.exists()`` is false for a dangling symlink.  Treat
                # every symlink companion as a pending auxiliary-state
                # failure so it cannot be silently ignored as "no intent".
                if intent_path.exists() or intent_path.is_symlink():
                    decision = recover_pending_identity_transaction(
                        self._state_path,
                        actual_ssh_revision=ssh_config.root_revision,
                        intent_path=intent_path,
                    )
                    if decision.action not in {
                        IdentityRecoveryAction.NO_PENDING,
                        IdentityRecoveryAction.FINALIZE_TARGET,
                        IdentityRecoveryAction.ABORT_BASE,
                    }:
                        self._enter_identity_degraded_locked(decision.reason)
                        self._publish_state_locked(
                            ssh_config, ConnectionFileState(), migrated=False
                        )
                        return
                state = read_identity_state_v2(self._state_path)
                if state is None:
                    raise _repository_error("Identity state disappeared during load")
                if state.observed_ssh_revision != ssh_config.root_revision:
                    state = reconcile_identity_state(
                        state,
                        projections,
                        ssh_config_revision=ssh_config.root_revision,
                        allow_tombstone_resurrection=allow_tombstone_resurrection,
                    )
                    write_identity_state_v2(self._state_path, state)
                self._identity_state = state
                self._identity_state_unavailable = False
                self._identity_recovery_required = False
                self._publish_state_locked(
                    ssh_config,
                    state_to_service_file_state(state, ssh_config.connections),
                    migrated=False,
                )
                return
            if kind is ConnectionStateFileKind.V1:
                legacy = read_connection_state(self._state_path)
                state, _report = migrate_v1_state(
                    legacy,
                    projections,
                    ssh_config_revision=ssh_config.root_revision,
                )
                write_identity_state_v2(self._state_path, state)
                self._identity_state = state
                self._identity_state_unavailable = False
                self._identity_recovery_required = False
                self._publish_state_locked(
                    ssh_config,
                    state_to_service_file_state(state, ssh_config.connections),
                    migrated=True,
                )
                return

            # A missing dedicated sidecar still accepts the historical
            # config.json migration, but it goes directly to v2.
            if kind is ConnectionStateFileKind.ABSENT:
                legacy = read_legacy_connection_state(self._legacy_config_path)
                legacy = self._reconcile_legacy_state(ssh_config, legacy)
                state, _report = migrate_v1_state(
                    legacy,
                    projections,
                    ssh_config_revision=ssh_config.root_revision,
                )
                write_identity_state_v2(self._state_path, state)
                self._identity_state = state
                self._identity_state_unavailable = False
                self._identity_recovery_required = False
                self._publish_state_locked(
                    ssh_config,
                    state_to_service_file_state(state, ssh_config.connections),
                    migrated=True,
                )
                return

            # Corrupt/unsupported state is never replaced by an empty v2
            # registry.  Keep SSH launch/read availability through an empty
            # decoration projection and make identity mutations fail safely.
            logger.warning(
                "Failed to load auxiliary connection state; continuing with SSH "
                "configuration only (reason=%s)",
                kind.value,
            )
            self._enter_identity_degraded_locked(kind.value)
            file_state = ConnectionFileState()
            self._publish_state_locked(ssh_config, file_state, migrated=False)
        except Exception as exc:
            failure_reason = (
                exc.diagnostic_category
                if isinstance(exc, CoreError)
                else type(exc).__name__
            )
            logger.warning(
                "Failed to load auxiliary connection state; continuing with "
                "SSH configuration only (reason=%s)",
                failure_reason,
            )
            self._enter_identity_degraded_locked(failure_reason)
            self._publish_state_locked(
                ssh_config,
                ConnectionFileState(),
                migrated=False,
            )

    def _enter_identity_degraded_locked(self, reason: str) -> None:
        """Drop any previously trusted UUID state after auxiliary failure."""
        self._identity_state = None
        self._identity_state_unavailable = True
        self._identity_recovery_required = True
        logger.warning("UUID identity state is degraded (reason=%s)", reason)

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
                    record.id, record.generation
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
                display_name=_display_name_for_record(record),
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
        if self._identity_state is None:
            return self._assemble(self._service, self._metadata, self._generation)
        return self._assemble_uuid_snapshot_locked()

    def _assemble_uuid_snapshot_locked(self) -> ConnectionStoreSnapshot:
        """Build an alias-compatible snapshot from UUID-owned sidecar state."""
        state = self._identity_state
        assert state is not None
        active = {
            item.projection.alias: item
            for item in state.identities
            if not item.tombstone
        }
        records = tuple(self._service.ordered_records())
        aliases = {record.id for record in records}
        alias_to_id = {record.id: record.id for record in records}
        def public_id(alias: str) -> str:
            return alias_to_id.get(alias, alias)

        group_names = {group.id: group.name for group in self._service.list_groups()}
        groups = tuple(
            GroupSummary(
                id=group.id,
                name=group.name,
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
                connection_ids=tuple(
                    public_id(cid) for cid in group.connection_ids if cid in aliases
                ),
            )
            for group in self._service.list_groups()
        )
        grouped = {cid for group in groups for cid in group.connection_ids}
        root = tuple(
            public_id(cid)
            for cid in self._service.root_order()
            if cid in aliases and public_id(cid) not in grouped
        )
        summaries = []
        for record in records:
            identity = active.get(record.id) if record.protocol == "ssh" else None
            public = record.id
            try:
                record_port = int(record.port)
            except (TypeError, ValueError):
                record_port = 22
            groups_for_record = tuple(
                GroupReference(id=group.id, name=group_names.get(group.id, ""))
                for group in groups
                if public in group.connection_ids
            )
            summaries.append(
                ConnectionSummary(
                    id=ConnectionId(public),
                    nickname=record.nickname,
                    host=record.host or str(record.data.get("host") or record.id),
                    hostname=record.hostname,
                    username=record.username,
                    port=(record_port if 1 <= record_port <= 65535 else 22),
                    protocol=record.protocol or "ssh",
                    health=ConnectionHealth.UNKNOWN,
                    groups=groups_for_record,
                    display_name=_display_name_for_record(record, identity),
                )
            )
        metadata = tuple(
            ConnectionMetadataSummary(
                connection_id=ConnectionId(public_id(cid)), values=values
            )
            for cid, values in self._metadata.items()
            if cid in aliases
        )
        return ConnectionStoreSnapshot(
            generation=self._generation,
            connections=tuple(summaries),
            groups=groups,
            root_connection_ids=root,
            metadata=metadata,
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
        self._load_state_locked()

    def _require_identity_state_locked(self) -> IdentityStateV2:
        if self._identity_state_unavailable or self._identity_state is None:
            raise CoreError(
                ErrorCode.CONNECTION_STATE_IO_ERROR,
                "UUID identity state is unavailable",
                diagnostic_category="identity_state_unavailable",
                diagnostic_reason="identity sidecar is corrupt or unsupported",
            )
        return self._identity_state

    def _resolve_internal_id_locked(self, connection_id: str) -> str:
        """Resolve an internal UUID or current alias to the service alias."""
        if self._service.get(connection_id) is not None:
            return connection_id
        if self._identity_state is not None:
            for identity in self._identity_state.identities:
                if not identity.tombstone and identity.uuid == connection_id:
                    return identity.projection.alias
        return connection_id

    def _resolve_public_id(self, connection_id: str) -> str:
        with self._lock:
            public_id = str(connection_id)
            self._assert_not_unresolved_locked(public_id)
            return self._resolve_internal_id_locked(public_id)

    def _assert_not_unresolved_locked(self, connection_id: str) -> None:
        state = self._identity_state
        if state is None:
            return
        aliases = {
            projection.alias
            for ambiguity in state.pending_ambiguities
            for projection in ambiguity.new_projections
        }
        if connection_id in aliases:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "Connection identity is unresolved for the current SSH configuration",
            )

    def public_id_for(self, connection_id: str) -> str:
        with self._lock:
            internal = self._resolve_internal_id_locked(connection_id)
            return internal

    def ssh_alias_for(self, connection_id: str) -> str:
        """Resolve an app identity to the current OpenSSH selector."""
        with self._lock:
            return self._resolve_internal_id_locked(str(connection_id))

    def _reconcile_loaded_identity_locked(
        self,
        ssh_config: LoadedSshConfiguration,
        *,
        explicit_continuity: Optional[Mapping[str, str]] = None,
        persist: bool = False,
    ) -> None:
        """Run the single reconciliation path for fresh loader data."""
        state = self._require_identity_state_locked()
        next_state = reconcile_identity_state(
            state,
            projections_from_records(ssh_config.connections),
            ssh_config_revision=ssh_config.root_revision,
            explicit_continuity=explicit_continuity or {},
        )
        self._identity_state = next_state
        self._loaded_ssh_config = ssh_config
        self._publish_state_locked(
            ssh_config,
            state_to_service_file_state(next_state, ssh_config.connections),
            migrated=False,
        )
        if persist and next_state != state:
            write_identity_state_v2(self._state_path, next_state)

    def _prepare_identity_intent_locked(
        self,
        prepared: Any,
        *,
        operation_label: str,
        explicit_continuity: Optional[Mapping[str, str]] = None,
        display_names: Optional[Mapping[str, str]] = None,
        target_transform: Optional[Callable[[IdentityStateV2], IdentityStateV2]] = None,
    ) -> IdentityTransactionIntent:
        """Build and durably record a complete target snapshot before SSH commit."""
        state = self._require_identity_state_locked()
        preview = self._ssh_store.preview_prepared(prepared)
        target = reconcile_identity_state(
            state,
            projections_from_records(preview.connections),
            ssh_config_revision=prepared.target_revision,
            explicit_continuity=explicit_continuity or {},
        )
        if display_names:
            target = replace(
                target,
                identities=tuple(
                    replace(identity, display_name=display_names.get(
                        identity.projection.alias, identity.display_name
                    ).strip())
                    if not identity.tombstone
                    and identity.projection.alias in display_names
                    else identity
                    for identity in target.identities
                ),
            )
        if target_transform is not None:
            target = target_transform(target)
        # A durable managed transaction always advances the sidecar exactly
        # once.  The reconciliation adapter already advances changed states,
        # but applying display-name continuity after reconciliation can make
        # an otherwise unchanged target different without changing its
        # generation.  Normalize both cases to the intent invariant rather
        # than allowing IdentityTransactionIntent to reject the request.
        target = replace(
            target,
            sidecar_generation=state.sidecar_generation + 1,
        )
        kind = "pending_ambiguity" if target.pending_ambiguities else "normal"
        intent = IdentityTransactionIntent(
            transaction_id=new_uuid4(),
            base_ssh_revision=prepared.base_revision,
            target_ssh_revision=prepared.target_revision,
            base_sidecar_generation=state.sidecar_generation,
            operation_label=operation_label,
            operation_kind=kind,
            target_state=target,
        )
        write_pending_identity_transaction(
            identity_transaction_intent_path(self._state_path), intent
        )
        return intent

    @staticmethod
    def _projection_semantics(projection) -> tuple:
        """Return the loader evidence that must agree across a commit."""
        return (
            projection.alias,
            projection.hostname,
            projection.port,
            projection.username,
            projection.identity_files,
            projection.destination_evidence,
            projection.username_literal,
            projection.username_is_explicit,
            projection.identity_file_evidence,
        )

    def _verify_committed_target_locked(
        self,
        intent: IdentityTransactionIntent,
        committed: LoadedSshConfiguration,
    ) -> None:
        """Reject a prepared target whose committed loader view differs."""
        if committed.root_revision != intent.target_ssh_revision:
            raise CoreError(
                ErrorCode.STALE_CONNECTION_STATE,
                "The committed SSH configuration differs from the prepared target",
            )
        actual = {
            item.alias: item
            for item in projections_from_records(committed.connections)
        }
        expected = {
            item.projection.alias: item.projection
            for item in intent.target_state.identities
            if not item.tombstone
        }
        for ambiguity in intent.target_state.pending_ambiguities:
            expected.update({
                item.alias: item for item in ambiguity.new_projections
            })
        if set(actual) - set(expected):
            raise CoreError(
                ErrorCode.STALE_CONNECTION_STATE,
                "The committed SSH configuration differs from the prepared target",
            )
        for alias, projection in actual.items():
            if self._projection_semantics(projection) != self._projection_semantics(
                expected[alias]
            ):
                raise CoreError(
                    ErrorCode.STALE_CONNECTION_STATE,
                    "The committed SSH configuration differs from the prepared target",
                )

    def _commit_identity_target_locked(
        self,
        intent: IdentityTransactionIntent,
        committed: LoadedSshConfiguration,
        captured,
    ) -> None:
        """Commit and record the exact target before clearing its intent.

        The sidecar post-write token is deliberately captured here, immediately
        after the target write and before any caller can clear ``.pending``.
        That keeps rollback able to restore both durable resources if intent
        cleanup fails after unlink or during its parent-directory fsync.
        """
        self._verify_committed_target_locked(intent, committed)
        # Keep the ordinary persistence hook observable for rollback/crash
        # tests, while making its input unambiguously the complete intent
        # target rather than a service-derived reconstruction.
        self._pending_identity_target = intent.target_state
        try:
            self._persist_state_file_locked()
        finally:
            self._pending_identity_target = None
        self._record_post_write_locked(captured, self._state_path)
        self._identity_state = intent.target_state
        self._identity_state_unavailable = False
        self._loaded_ssh_config = committed
        self._publish_state_locked(
            committed,
            state_to_service_file_state(
                intent.target_state, committed.connections
            ),
            migrated=False,
        )

    def _duplicate_target_placement(
        self,
        source_alias: str,
        duplicate_alias: str,
    ) -> Callable[[IdentityStateV2], IdentityStateV2]:
        """Build the complete UUID placement for a duplicate before intent."""
        state = self._require_identity_state_locked()
        source = next(
            item for item in state.identities
            if not item.tombstone and item.projection.alias == source_alias
        )

        def transform(target: IdentityStateV2) -> IdentityStateV2:
            duplicate = next(
                item for item in target.identities
                if not item.tombstone and item.projection.alias == duplicate_alias
            )
            source_ref = ConnectionReference(ReferenceKind.SSH_UUID, source.uuid)
            duplicate_ref = ConnectionReference(ReferenceKind.SSH_UUID, duplicate.uuid)
            groups = []
            grouped = set()
            for group in target.groups:
                members = list(group.members)
                if source_ref in members and duplicate_ref not in members:
                    members.append(duplicate_ref)
                grouped.update(members)
                groups.append(replace(group, members=tuple(members)))
            roots = [
                item for item in target.root_connections
                if item != duplicate_ref
            ]
            if source_ref not in grouped and duplicate_ref not in roots:
                roots.append(duplicate_ref)
            return replace(
                target,
                groups=tuple(groups),
                root_connections=tuple(roots),
            )

        return transform

    def _finish_identity_intent_locked(self) -> None:
        clear_pending_identity_transaction(identity_transaction_intent_path(self._state_path))

    def _sync_identity_placements_from_service_locked(
        self,
        *,
        explicit_continuity: Optional[Mapping[str, str]] = None,
    ) -> IdentityStateV2:
        """Convert temporary alias service placement back to UUID references."""
        state = self._require_identity_state_locked()
        records = tuple(self._service.ordered_records())
        alias_to_uuid = {
            identity.projection.alias: identity.uuid
            for identity in state.identities
            if not identity.tombstone
        }
        for old_alias, new_alias in (explicit_continuity or {}).items():
            identity = next(
                (
                    item
                    for item in state.identities
                    if not item.tombstone and item.projection.alias == old_alias
                ),
                None,
            )
            if identity is not None:
                alias_to_uuid[new_alias] = identity.uuid
        non_ssh_ids = {
            record.id for record in records if record.protocol != "ssh"
        }
        def reference(cid: str) -> Optional[ConnectionReference]:
            if cid in alias_to_uuid:
                return ConnectionReference(ReferenceKind.SSH_UUID, alias_to_uuid[cid])
            if cid in non_ssh_ids:
                return ConnectionReference(ReferenceKind.NON_SSH_ID, cid)
            return None

        old_groups = {group.id: group for group in state.groups}
        groups = []
        for group in self._service.list_groups():
            members = []
            for cid in group.connection_ids:
                item = reference(cid)
                if item is not None and item not in members:
                    members.append(item)
            # Keep unresolved old UUID ownership in place until ambiguity is
            # explicitly resolved; service projection cannot render it.
            previous = old_groups.get(group.id)
            if previous is not None:
                for item in previous.members:
                    if item not in members:
                        if item.kind is ReferenceKind.SSH_UUID and any(
                            old.uuid == item.value and not old.tombstone
                            for old in state.identities
                        ):
                            members.append(item)
            groups.append(
                UuidGroupState(
                    id=group.id,
                    name=group.name,
                    members=tuple(members),
                    parent_id=group.parent_id,
                    order=group.order,
                    color=group.color,
                )
            )
        grouped = {item for group in groups for item in group.members}
        roots = []
        for cid in self._service.root_order():
            item = reference(cid)
            if item is not None and item not in grouped and item not in roots:
                roots.append(item)
        # Preserve placement of unresolved old active identities.
        for item in state.root_connections:
            if item not in grouped and item not in roots:
                if item.kind is ReferenceKind.NON_SSH_ID and item.value not in non_ssh_ids:
                    continue
                if item.kind is ReferenceKind.SSH_UUID and not any(
                    old.uuid == item.value and not old.tombstone
                    for old in state.identities
                ):
                    continue
                roots.append(item)
        current_aliases = {
            record.id for record in records if record.protocol == "ssh"
        }
        identities_by_uuid = {identity.uuid: identity for identity in state.identities}
        metadata = {}
        # Preserve UUID-owned state that cannot currently be projected because
        # its alias is part of a pending ambiguity.  Only a visible active
        # alias whose metadata is absent is treated as an explicit removal.
        for uuid, values in state.metadata.items():
            identity = identities_by_uuid.get(uuid)
            if (
                identity is None
                or identity.tombstone
                or identity.projection.alias not in current_aliases
                or identity.projection.alias in self._metadata
            ):
                metadata[uuid] = values
        for cid, values in self._metadata.items():
            if cid in alias_to_uuid:
                metadata[alias_to_uuid[cid]] = values
            elif cid in non_ssh_ids:
                # Non-SSH metadata is stored in its own namespace below.
                continue
        non_ssh_metadata = dict(state.non_ssh_metadata)
        for record in records:
            if record.protocol != "ssh" and record.id in self._metadata:
                non_ssh_metadata[record.id] = self._metadata[record.id]
        candidate = replace(
            state,
            groups=tuple(groups),
            root_connections=tuple(roots),
            metadata=metadata,
            non_ssh_connections=tuple(
                copy.deepcopy(record.data)
                for record in records
                if record.protocol != "ssh"
            ),
            non_ssh_metadata=non_ssh_metadata,
        )
        if candidate != state:
            candidate = replace(candidate, sidecar_generation=state.sidecar_generation + 1)
        self._identity_state = candidate
        return candidate

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
        try:
            nickname = validate_ssh_host_alias(nickname)
        except ValueError as error:
            raise CoreError(ErrorCode.VALIDATION_ERROR, str(error)) from error
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
            try:
                validate_ssh_host_alias(str(payload.get("nickname") or ""))
            except ValueError as error:
                raise CoreError(ErrorCode.VALIDATION_ERROR, str(error)) from error
            protocol = str(payload.get("protocol") or "ssh").strip() or "ssh"
            try:
                if protocol == "ssh":
                    self._sync_identity_placements_from_service_locked()
                    ssh_payload = dict(payload)
                    ssh_payload.pop("display_name", None)
                    prepared = self._ssh_store.prepare_create(ssh_payload)
                    requested_name = str(payload.get("display_name") or "").strip()
                    intent = self._prepare_identity_intent_locked(
                        prepared,
                        operation_label="create",
                        display_names=(
                            {str(payload.get("nickname")): requested_name}
                            if requested_name else None
                        ),
                    )
                    result = self._ssh_store.commit_prepared(prepared)
                    self._record_post_write_locked(
                        disk_before, Path(result.touched_path)
                    ) if result.touched_path else None
                    committed = self._ssh_store.load()
                    self._commit_identity_target_locked(intent, committed, disk_before)
                    created = self._service.get(result.connection_id)
                    if created is None:
                        raise _repository_error("The committed connection was not found")
                else:
                    nickname = self._validate_new_nickname(
                        str(payload.get("nickname") or "")
                    )
                    payload["id"] = nickname
                    payload["nickname"] = nickname
                    payload["generation"] = 1
                    created = self._service.create(payload)
                    self._non_ssh_generations[created.id] = 1
                # Root order / membership / metadata always live in
                # connections.json; persist it after every committed mutation.
                if protocol != "ssh":
                    self._persist_state_file_locked()
                if protocol == "ssh":
                    self._finish_identity_intent_locked()
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
            connection_id = self._resolve_internal_id_locked(connection_id)
            self._assert_not_unresolved_locked(connection_id)
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
            if new_nick:
                try:
                    validate_ssh_host_alias(new_nick)
                except ValueError as error:
                    raise CoreError(ErrorCode.VALIDATION_ERROR, str(error)) from error
            if new_nick and new_nick != connection_id:
                self._validate_new_nickname(new_nick)
            try:
                if protocol == "ssh":
                    self._sync_identity_placements_from_service_locked()
                    prepared = self._ssh_store.prepare_update(
                        connection_id,
                        payload,
                        expected_generation=expected_generation,
                    )
                    planned_name = str(payload.get("nickname") or connection_id)
                    planned_continuity = (
                        {connection_id: planned_name}
                        if planned_name != connection_id else {}
                    )
                    intent = self._prepare_identity_intent_locked(
                        prepared,
                        operation_label="update",
                        explicit_continuity=planned_continuity,
                        display_names=(
                            {planned_name: str(payload["display_name"]).strip()}
                            if payload.get("display_name")
                            else None
                        ),
                    )
                    result = self._ssh_store.commit_prepared(prepared)
                    new_id = result.connection_id
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
                    committed = self._ssh_store.load()
                    self._commit_identity_target_locked(intent, committed, disk_before)
                    updated = self._service.get(new_id)
                    if updated is None:
                        raise _repository_error("The committed connection was not found")
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
                    current_generation = self._non_ssh_generations.get(
                        connection_id, existing.generation
                    )
                    next_generation = current_generation + 1
                    payload["generation"] = next_generation
                    updated = self._service.update(connection_id, payload)
                    if new_nick and new_nick != connection_id:
                        self._non_ssh_generations.pop(connection_id, None)
                        self._non_ssh_generations[new_nick] = next_generation
                    else:
                        self._non_ssh_generations[connection_id] = next_generation
                if protocol != "ssh":
                    self._migrate_metadata_on_rename(connection_id, updated.id)
                    self._persist_state_file_locked()
                if protocol == "ssh":
                    self._finish_identity_intent_locked()
            except Exception:
                self._rollback_after_failure_locked(disk_before)
                raise
            self._commit(before)
            return self._service.get(updated.id)

    def duplicate_connection(self, connection_id: str) -> ConnectionRecord:
        with self._mutation_scope():
            before = self._begin()
            connection_id = self._resolve_internal_id_locked(connection_id)
            self._assert_not_unresolved_locked(connection_id)
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
                    self._sync_identity_placements_from_service_locked()
                    prepared = self._ssh_store.prepare_duplicate(connection_id)
                    intent = self._prepare_identity_intent_locked(
                        prepared,
                        operation_label="duplicate",
                        target_transform=self._duplicate_target_placement(
                            connection_id, prepared.connection_id
                        ),
                    )
                    result = self._ssh_store.commit_prepared(prepared)
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
                    committed = self._ssh_store.load()
                    self._commit_identity_target_locked(intent, committed, disk_before)
                    created = self._service.get(result.connection_id)
                    if created is None:
                        raise _repository_error("The committed connection was not found")
                else:
                    created = self._service.duplicate(connection_id)
                    self._non_ssh_generations[created.id] = 1
                if existing.protocol != "ssh":
                    # Mirror the source's group placement on the duplicate.
                    for gid in self._service.group_ids_of(connection_id):
                        self._service.copy_connection_to_group(created.id, gid)
                    self._persist_state_file_locked()
                if existing.protocol == "ssh":
                    self._finish_identity_intent_locked()
            except Exception:
                self._rollback_after_failure_locked(disk_before)
                raise
            self._commit(before)
            return self._service.get(created.id)

    def delete_connection(self, connection_id: str) -> None:
        with self._mutation_scope():
            before = self._begin()
            connection_id = self._resolve_internal_id_locked(connection_id)
            self._assert_not_unresolved_locked(connection_id)
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
                    self._sync_identity_placements_from_service_locked()
                    prepared = self._ssh_store.prepare_delete(connection_id)
                    intent = self._prepare_identity_intent_locked(
                        prepared, operation_label="delete"
                    )
                    result = self._ssh_store.commit_prepared(prepared)
                    if result.touched_path:
                        self._record_post_write_locked(
                            disk_before, Path(result.touched_path),
                        )
                    committed = self._ssh_store.load()
                    self._commit_identity_target_locked(intent, committed, disk_before)
                else:
                    self._service.delete(connection_id)
                    self._non_ssh_generations.pop(connection_id, None)
                if existing.protocol != "ssh":
                    self._remove_persisted_root_id_locked(connection_id)
                    self._metadata.pop(connection_id, None)
                    self._persist_state_file_locked()
                if existing.protocol == "ssh":
                    self._finish_identity_intent_locked()
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
            connection_id = self._resolve_internal_id_locked(connection_id)
            self._assert_not_unresolved_locked(connection_id)
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
                prepared = self._ssh_store.prepare_split(
                    connection_id,
                    original_host_token,
                    data,
                    expected_generation=expected_generation,
                )
                preview = self._ssh_store.preview_prepared(prepared)
                continuity = (
                    {connection_id: prepared.connection_id}
                    if connection_id not in {item.id for item in preview.connections}
                    and prepared.connection_id != connection_id
                    else {}
                )
                intent = self._prepare_identity_intent_locked(
                    prepared,
                    operation_label="split",
                    explicit_continuity=continuity,
                )
                result = self._ssh_store.commit_prepared(prepared)
                if result.touched_path:
                    self._record_post_write_locked(
                        disk_before, Path(result.touched_path),
                    )
                committed = self._ssh_store.load()
                self._commit_identity_target_locked(intent, committed, disk_before)
                result_id = result.connection_id
                self._finish_identity_intent_locked()
            except Exception:
                self._rollback_after_failure_locked(disk_before)
                raise
            self._commit(before)
            return self._service.get(result_id)

    def _persist_state_file_locked(
        self, target_state: Optional[IdentityStateV2] = None
    ) -> None:
        if target_state is None:
            target_state = self._pending_identity_target
        state = (
            target_state
            if target_state is not None
            else self._sync_identity_placements_from_service_locked()
        )
        write_identity_state_v2(self._state_path, state)

    def _remove_persisted_root_id_locked(self, connection_id: str) -> None:
        """Forget a root reference removed by an explicit managed delete."""
        self._persisted_root_order = tuple(
            cid for cid in self._persisted_root_order if cid != connection_id
        )

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
            for cid in request.connection_ids:
                self._assert_not_unresolved_locked(str(cid))
            if request.target_connection_id is not None:
                self._assert_not_unresolved_locked(
                    str(request.target_connection_id)
                )
            internal_ids = tuple(
                self._resolve_internal_id_locked(cid)
                for cid in request.connection_ids
            )
            internal_target = (
                self._resolve_internal_id_locked(request.target_connection_id)
                if request.target_connection_id is not None else None
            )
            if internal_ids != request.connection_ids or internal_target != request.target_connection_id:
                request = replace(
                    request,
                    connection_ids=internal_ids,
                    target_connection_id=internal_target,
                )
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
        connection_id = self._resolve_public_id(connection_id)
        return self._group_mutation(
            lambda: self._service.assign_group(connection_id, group_id)
        )

    def copy_connection_to_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        connection_id = self._resolve_public_id(connection_id)
        return self._group_mutation(
            lambda: self._service.copy_connection_to_group(connection_id, group_id)
        )

    def remove_connection_from_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        connection_id = self._resolve_public_id(connection_id)
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
        connection_id = self._resolve_public_id(connection_id)
        target_connection_id = self._resolve_public_id(target_connection_id)
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
            connection_id = self._resolve_internal_id_locked(connection_id)
            self._assert_not_unresolved_locked(connection_id)
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

    def set_display_name(
        self,
        connection_id: str,
        display_name: str,
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        """Persist an app-owned name without changing SSH configuration."""
        if (
            type(display_name) is not str
            or not display_name.strip()
            or len(display_name) > MAX_DISPLAY_NAME_LENGTH
        ):
            raise CoreError(ErrorCode.VALIDATION_ERROR, "A display name is required")
        with self._mutation_scope():
            before = self._begin()
            record = self._service.get(connection_id)
            if record is None:
                raise CoreError(
                    ErrorCode.CONNECTION_NOT_FOUND,
                    "The connection does not exist",
                )
            if record.protocol != "ssh":
                current_generation = self._non_ssh_generations.get(
                    record.id, record.generation
                )
                if (
                    expected_generation is not None
                    and expected_generation != current_generation
                ):
                    raise CoreError(
                        ErrorCode.STALE_CONNECTION_STATE,
                        "The connection changed during the display name operation",
                    )
                normalized = display_name.strip()
                if _display_name_for_record(record) == normalized:
                    return record
                next_generation = current_generation + 1
                updated = self._service.update(
                    record.id,
                    {"display_name": normalized, "generation": next_generation},
                )
                self._non_ssh_generations[updated.id] = next_generation
                try:
                    self._persist_state_file_locked()
                except Exception:
                    self._resync_from_files()
                    raise
                self._commit(before)
                return updated
            previous = self._identity_state
            internal = self._set_display_name_locked(connection_id, display_name)
            if self._identity_state == previous:
                return self._service.get(internal)
            try:
                write_identity_state_v2(self._state_path, self._identity_state)
            except Exception:
                # Keep the in-memory projection aligned with whichever durable
                # sidecar state won the write.  In particular, a post-replace
                # fsync failure means durability is unknown; re-reading is
                # safer than assuming the old bytes still won.
                try:
                    self._resync_from_files()
                except Exception:
                    self._identity_state = previous
                raise
            self._commit(before)
            return self._service.get(internal)

    def _set_display_name_locked(
        self, connection_id: str, display_name: str, *, bump: bool = True
    ) -> str:
        state = self._require_identity_state_locked()
        self._assert_not_unresolved_locked(str(connection_id))
        internal = self._resolve_internal_id_locked(connection_id)
        identities = []
        found = False
        for identity in state.identities:
            if not identity.tombstone and identity.projection.alias == internal:
                found = True
                normalized = display_name.strip()
                identities.append(
                    identity
                    if identity.display_name == normalized
                    else replace(identity, display_name=normalized)
                )
            else:
                identities.append(identity)
        if not found:
            raise CoreError(ErrorCode.CONNECTION_NOT_FOUND, "The connection does not exist")
        if tuple(identities) == state.identities:
            return internal
        self._identity_state = replace(
            state,
            identities=tuple(identities),
            sidecar_generation=(
                state.sidecar_generation + 1 if bump else state.sidecar_generation
            ),
        )
        return internal

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
            requested = tuple(self._resolve_public_id(cid) for cid in requested)
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
    # Logical backup export/restore (portable user state only — never a
    # copy of connections.json, never internal UUID/sidecar bookkeeping)
    # ------------------------------------------------------------------

    def snapshot_for_backup(self) -> Dict[str, Any]:
        """Portable ``connection_store`` backup section for the current state.

        Built entirely from :meth:`snapshot`'s public projection: SSH aliases
        and non-SSH nicknames as ids, never internal UUIDs or sidecar
        bookkeeping (``observed_ssh_revision``, ``sidecar_generation``, etc).
        """
        with self._lock:
            snap = self._build_snapshot_locked()
            records_by_id = {
                record.id: record for record in self._service.ordered_records()
            }

        def _connection_entry(c: ConnectionSummary) -> Dict[str, Any]:
            entry: Dict[str, Any] = {
                "id": str(c.id),
                "protocol": c.protocol,
                "nickname": c.nickname,
                "hostname": c.hostname,
                "username": c.username,
                "port": c.port,
                "display_name": (
                    "" if c.display_name == c.nickname else c.display_name
                ),
            }
            record = records_by_id.get(str(c.id))
            if record is not None and c.protocol != "ssh":
                plugin_data = _portable_plugin_data(c.protocol, record.data)
                if plugin_data:
                    entry["plugin_data"] = plugin_data
            return entry

        return {
            "version": CONNECTION_STORE_SECTION_VERSION,
            "connections": [_connection_entry(c) for c in snap.connections],
            "groups": [
                {
                    "id": str(g.id),
                    "name": g.name,
                    "parent_id": (str(g.parent_id) if g.parent_id is not None else None),
                    "order": g.order,
                    "color": g.color,
                    "connection_ids": [str(cid) for cid in g.connection_ids],
                }
                for g in snap.groups
            ],
            "root_connection_ids": [str(cid) for cid in snap.root_connection_ids],
            "metadata": [
                {"connection_id": str(m.connection_id), "values": dict(m.values)}
                for m in snap.metadata
            ],
        }

    def _validate_connection_store_section_locked(
        self,
        connections: List[Mapping[str, Any]],
        groups: List[Mapping[str, Any]],
        root_ids: List[Any],
        metadata: List[Mapping[str, Any]],
    ) -> None:
        """Reuse ``ConnectionStoreSnapshot``'s own cross-validation for free:
        duplicate ids, unknown group parents/members, parent cycles, orphan
        metadata, and unknown/misplaced root references are all rejected by
        its constructor — no need to hand-roll those checks here."""
        conn_summaries = tuple(
            ConnectionSummary(
                id=ConnectionId(str(c["id"])),
                nickname=str(c.get("nickname") or c["id"]),
                host=str(c.get("hostname") or c["id"]),
                hostname=str(c.get("hostname") or ""),
                username=str(c.get("username") or ""),
                port=int(c.get("port") or 22),
                protocol=str(c.get("protocol") or "ssh"),
                display_name=str(c.get("display_name") or ""),
            )
            for c in connections
        )
        group_summaries = tuple(
            GroupSummary(
                id=str(g["id"]),
                name=str(g.get("name") or g["id"]),
                parent_id=(str(g["parent_id"]) if g.get("parent_id") else None),
                order=int(g.get("order") or 0),
                color=str(g.get("color") or ""),
                connection_ids=tuple(str(cid) for cid in (g.get("connection_ids") or [])),
            )
            for g in groups
        )
        metadata_summaries = tuple(
            ConnectionMetadataSummary(
                connection_id=ConnectionId(str(m["connection_id"])),
                values=m.get("values") or {},
            )
            for m in metadata
        )
        ConnectionStoreSnapshot(
            generation=0,
            connections=conn_summaries,
            groups=group_summaries,
            root_connection_ids=tuple(str(cid) for cid in root_ids),
            metadata=metadata_summaries,
        )

    def _restore_non_ssh_connection_locked(
        self, entry: Mapping[str, Any], *, mode: str, warnings: List[str]
    ) -> None:
        cid = str(entry["id"])
        existing = self._service.get(cid)
        protocol = str(entry.get("protocol") or "telnet")
        payload: Dict[str, Any] = {
            "id": cid,
            "nickname": str(entry.get("nickname") or cid),
            "hostname": str(entry.get("hostname") or ""),
            "username": str(entry.get("username") or ""),
            "port": int(entry.get("port") or 22),
            "protocol": protocol,
        }
        display_name = str(entry.get("display_name") or "").strip()
        if display_name:
            payload["display_name"] = display_name
        raw_plugin_data = entry.get("plugin_data")
        if isinstance(raw_plugin_data, Mapping):
            # Re-filter on restore too: never trust a hand-crafted or
            # cross-version backup file to have applied the same allowlist/
            # security filter used on export.
            payload.update(_portable_plugin_data(protocol, raw_plugin_data))
        if existing is None:
            try:
                payload["generation"] = 1
                created = self._service.create(payload)
                self._non_ssh_generations[created.id] = 1
            except Exception as error:
                warnings.append(f"Could not restore connection {cid!r}: {error}")
        elif mode == "replace":
            try:
                next_generation = (
                    self._non_ssh_generations.get(cid, existing.generation) + 1
                )
                payload["generation"] = next_generation
                self._service.update(cid, payload)
                self._non_ssh_generations[cid] = next_generation
            except Exception as error:
                warnings.append(f"Could not update connection {cid!r}: {error}")
        # merge mode + already-present connection: left untouched (non-destructive).

    def _restore_groups_locked(
        self, imported_groups: List[Mapping[str, Any]], *, mode: str, warnings: List[str]
    ) -> Dict[str, str]:
        """Map each imported group id to a local group id, creating/reusing
        groups by ``(mapped_parent_id, name)`` — imported ids are never
        treated as stable local targets, since group ids are locally
        generated (``generate_group_slug``)."""
        existing_by_key: Dict[Tuple[Optional[str], str], str] = {
            (group.parent_id, group.name): group.id
            for group in self._service.list_groups()
        }
        by_id = {str(g["id"]): g for g in imported_groups}
        id_map: Dict[str, str] = {}

        # No cycle guard needed here: restore_connection_store already ran
        # imported_groups through ConnectionStoreSnapshot's own validation
        # (_validate_connection_store_section_locked), which rejects any
        # parent cycle among these same id/parent_id pairs before this method
        # is ever called — so this recursion is guaranteed to terminate.
        def resolve(imported_id: str) -> Optional[str]:
            if imported_id in id_map:
                return id_map[imported_id]
            entry = by_id.get(imported_id)
            if entry is None:
                return None
            parent_imported_id = entry.get("parent_id")
            mapped_parent = (
                resolve(str(parent_imported_id)) if parent_imported_id else None
            )
            name = str(entry.get("name") or imported_id).strip() or imported_id
            color = str(entry.get("color") or "")
            key = (mapped_parent, name)
            local_id = existing_by_key.get(key)
            if local_id is None:
                try:
                    record = self._service.create_group(
                        name, parent_id=mapped_parent, color=color
                    )
                    local_id = record.id
                    existing_by_key[key] = local_id
                except Exception as error:
                    warnings.append(f"Could not restore group {name!r}: {error}")
                    return None
            elif mode == "replace":
                try:
                    self._service.set_group_color(local_id, color)
                except Exception as error:
                    warnings.append(f"Could not update group {name!r}: {error}")
            id_map[imported_id] = local_id
            return local_id

        for imported_id in by_id:
            resolve(imported_id)

        if mode == "replace":
            kept = set(id_map.values())
            for group in list(self._service.list_groups()):
                if group.id not in kept:
                    try:
                        self._service.delete_group(group.id)
                    except Exception as error:
                        warnings.append(
                            f"Could not remove group {group.id!r}: {error}"
                        )

        # Apply the backup's sibling order (user-visible state that must
        # survive the round trip) among the groups it describes. Siblings not
        # mentioned in the backup are left wherever place_group's insertion
        # puts them relative to this batch — this only guarantees the
        # relative order *among backup-described siblings*, in both modes.
        by_parent: Dict[Optional[str], List[Tuple[int, str, str]]] = {}
        for imported_id, local_id in id_map.items():
            entry = by_id[imported_id]
            parent_imported_id = entry.get("parent_id")
            mapped_parent = (
                id_map.get(str(parent_imported_id)) if parent_imported_id else None
            )
            order_value = entry.get("order")
            order_value = order_value if isinstance(order_value, int) else 0
            by_parent.setdefault(mapped_parent, []).append(
                (order_value, str(entry.get("name") or ""), local_id)
            )
        for parent_id, siblings in by_parent.items():
            siblings.sort(key=lambda item: (item[0], item[1]))
            for index, (_, _, local_id) in enumerate(siblings):
                try:
                    self._service.place_group(local_id, parent_id, index)
                except Exception as error:
                    warnings.append(f"Could not order group {local_id!r}: {error}")

        return id_map

    def _restore_membership_and_order_locked(
        self,
        imported_groups: List[Mapping[str, Any]],
        imported_root: List[Any],
        id_map: Dict[str, str],
        *,
        mode: str,
        imported_connection_ids: set,
        warnings: List[str],
    ) -> None:
        if mode == "replace":
            # Evict stale membership from groups the backup persists: a
            # connection the backup describes must end up in exactly the
            # group(s) the backup states, not additionally in whatever
            # group(s) it happened to occupy locally beforehand. Scoped to
            # connections the backup actually describes (imported_connection_
            # ids) — a local-only connection outside the backup's scope keeps
            # its existing membership untouched, since the backup takes no
            # position on it. (Multi-group membership the backup itself
            # declares survives: a connection stays "stale" for a group only
            # when that group's own backup-declared members omit it.)
            for entry in imported_groups:
                local_group_id = id_map.get(str(entry["id"]))
                if local_group_id is None:
                    continue
                desired = {str(cid) for cid in (entry.get("connection_ids") or [])}
                local_group = next(
                    (g for g in self._service.list_groups() if g.id == local_group_id),
                    None,
                )
                if local_group is None:
                    continue
                stale = (set(local_group.connection_ids) - desired) & imported_connection_ids
                for cid in stale:
                    try:
                        self._service.remove_connection_from_group(cid, local_group_id)
                    except Exception as error:
                        warnings.append(
                            f"Could not remove stale membership for {cid!r}: {error}"
                        )

        for entry in imported_groups:
            local_group_id = id_map.get(str(entry["id"]))
            if local_group_id is None:
                continue
            requested = [str(cid) for cid in (entry.get("connection_ids") or [])]
            member_ids = tuple(
                cid for cid in requested if self._service.get(cid) is not None
            )
            for cid in set(requested) - set(member_ids):
                warnings.append(
                    f"Connection {cid!r} referenced by a restored group was not found"
                )
            if member_ids:
                self._service.move_connections(
                    MoveConnectionsRequest(
                        connection_ids=member_ids,
                        target_group_id=local_group_id,
                        mode=ConnectionPlacementMode.ADDITIVE,
                    )
                )

        requested_root = [str(cid) for cid in imported_root]
        root_ids = tuple(
            cid for cid in requested_root if self._service.get(cid) is not None
        )
        for cid in set(requested_root) - set(root_ids):
            warnings.append(
                f"Root connection {cid!r} referenced by the backup was not found"
            )
        if mode == "merge":
            # Merge is non-destructive: a connection that already belongs to
            # a group (whether from pre-existing target state or from this
            # same restore's own group membership above) must keep that
            # membership. Root/group membership are mutually exclusive in
            # this domain model, so an exclusive move to root would evict it
            # — instead, the backup's root placement is a no-op for an
            # already-grouped connection under merge.
            root_ids = tuple(
                cid for cid in root_ids
                if self._service.get(cid).group_id is None
            )
        if root_ids:
            self._service.move_connections(
                MoveConnectionsRequest(connection_ids=root_ids)
            )

    def _restore_metadata_locked(
        self,
        imported_metadata: List[Mapping[str, Any]],
        imported_connection_ids: set,
        *,
        mode: str,
        warnings: List[str],
    ) -> None:
        seen: set = set()
        for entry in imported_metadata:
            cid = str(entry.get("connection_id"))
            if self._service.get(cid) is None:
                warnings.append(f"Metadata for unknown connection {cid!r} was skipped")
                continue
            seen.add(cid)
            values = entry.get("values") or {}
            try:
                if mode == "replace":
                    merged = validate_safe_metadata(dict(values)) if values else {}
                else:
                    current = thaw_safe_metadata(self._metadata.get(cid, {}))
                    current.update(values)
                    merged = validate_safe_metadata(current)
                if merged:
                    self._metadata[cid] = merged
                else:
                    self._metadata.pop(cid, None)
            except Exception as error:
                warnings.append(f"Could not restore metadata for {cid!r}: {error}")
        if mode == "replace":
            for cid in imported_connection_ids - seen:
                self._metadata.pop(cid, None)

    def restore_connection_store(
        self, section: Mapping[str, Any], *, mode: str = "merge"
    ) -> ConnectionStoreRestoreResult:
        """Apply a portable ``connection_store`` backup section.

        Restores non-SSH connections, groups, group membership, root order,
        and safe metadata through the same in-memory primitives used by the
        repository's own public mutators — never by writing
        ``connections.json`` directly. Runs as one atomic transaction:
        ``mode="replace"`` additionally removes local non-SSH connections,
        groups, and metadata absent from the backup; ``mode="merge"`` never
        deletes. Returns immediately, with a warning, for a section whose
        ``version`` this repository does not understand — this keeps a
        newer-than-supported backup from hard-failing the rest of an import.
        """
        if mode not in ("merge", "replace"):
            raise ValueError("mode must be 'merge' or 'replace'")
        if not isinstance(section, Mapping):
            raise TypeError("connection_store section must be a mapping")

        version = section.get("version")
        if type(version) is not int or version > CONNECTION_STORE_SECTION_VERSION:
            return ConnectionStoreRestoreResult(
                warnings=("connection_store section version is unsupported; skipped",)
            )

        imported_connections = list(section.get("connections") or [])
        imported_groups = list(section.get("groups") or [])
        imported_root = list(section.get("root_connection_ids") or [])
        imported_metadata = list(section.get("metadata") or [])

        warnings: List[str] = []
        with self._mutation_scope():
            before = self._begin()
            try:
                # Reload from disk first: BackupManager writes the SSH config
                # tree to disk before invoking this restore, but this
                # repository's in-memory state still predates that write.
                # Resolving SSH aliases against stale state would silently
                # drop every freshly-restored SSH connection from group/
                # root-order/metadata references.
                self._load_state_locked()

                self._validate_connection_store_section_locked(
                    imported_connections, imported_groups, imported_root,
                    imported_metadata,
                )

                imported_ids = {str(c["id"]) for c in imported_connections}
                for entry in imported_connections:
                    if str(entry.get("protocol") or "ssh") == "ssh":
                        cid = str(entry["id"])
                        display_name = str(entry.get("display_name") or "").strip()
                        if display_name and self._service.get(cid) is not None:
                            try:
                                self._set_display_name_locked(
                                    cid, display_name, bump=False
                                )
                            except CoreError as error:
                                warnings.append(
                                    f"Could not restore display name for {cid!r}: {error}"
                                )
                        continue
                    self._restore_non_ssh_connection_locked(
                        entry, mode=mode, warnings=warnings
                    )

                if mode == "replace":
                    for record in list(self._service.ordered_records()):
                        if record.protocol != "ssh" and record.id not in imported_ids:
                            try:
                                self._service.delete(record.id)
                                self._non_ssh_generations.pop(record.id, None)
                            except Exception as error:
                                warnings.append(
                                    f"Could not remove connection {record.id!r}: {error}"
                                )

                id_map = self._restore_groups_locked(
                    imported_groups, mode=mode, warnings=warnings
                )
                self._restore_membership_and_order_locked(
                    imported_groups, imported_root, id_map,
                    mode=mode, imported_connection_ids=imported_ids,
                    warnings=warnings,
                )
                self._restore_metadata_locked(
                    imported_metadata, imported_ids, mode=mode, warnings=warnings
                )

                self._persist_state_file_locked()
            except Exception:
                self._resync_from_files()
                raise
            self._commit(before)
        return ConnectionStoreRestoreResult(warnings=tuple(warnings))

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
