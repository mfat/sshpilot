"""Minimal in-memory ``ConnectionRepositoryProtocol`` fake for test use.

Does not import ``Config``, ``ConnectionManager``, ``GroupManager``,
or GTK.  Stores real ``ConnectionRecord`` instances, produces valid
``ConnectionStoreSnapshot`` objects, maintains a listener list, and
publishes ``RepositoryChange`` events after each mutation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from sshpilot.core.connections.models import ConnectionRecord, GroupRecord
from sshpilot.core.connections.repository import RepositoryChange
from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.api.models.connection_store import ConnectionStoreSnapshot, GroupSummary
from sshpilot.api.models.connections import (
    ConnectionHealth,
    ConnectionSummary,
    GroupReference,
)
from sshpilot.api.models.common import ConnectionId

if TYPE_CHECKING:
    from sshpilot.core.connection_application_service import ConnectionApplicationService


class FakeConnectionRepository:
    """Minimal ``ConnectionRepositoryProtocol`` fake.

    ``snapshot``, ``list_records``, ``get_record``, ``get_editor_record``,
    ``discover_paths``, ``reload``, ``add_listener``, and ``remove_listener``
    are always available.  Callers can inject failure via ``fail_next`` for
    the next mutation.
    """

    def __init__(self, records: Optional[List[ConnectionRecord]] = None) -> None:
        self._records: Dict[str, ConnectionRecord] = {}
        self._groups: Dict[str, GroupRecord] = {}
        self._listeners: list = []
        self._generation = 0
        self.fail_next: bool = False
        self.last_update: Optional[Dict[str, Any]] = None
        for record in records or []:
            self._records[record.id] = record

    # -- read surface -------------------------------------------------------

    def snapshot(self) -> ConnectionStoreSnapshot:
        return self._build_snapshot()

    def list_records(self) -> Tuple[ConnectionRecord, ...]:
        return tuple(self._records.values())

    def get_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        return self._records.get(connection_id)

    def get_editor_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        return self._records.get(connection_id)

    def discover_paths(self) -> frozenset:
        return frozenset()

    def reload(self) -> ConnectionStoreSnapshot:
        return self._build_snapshot()

    def get_ssh_config_text(self):
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "SSH config text editing is unavailable in this test backend",
        )

    def save_ssh_config_text(self, request):
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "SSH config text editing is unavailable in this test backend",
        )

    # -- listeners ----------------------------------------------------------

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    # -- mutations ----------------------------------------------------------

    def create_connection(self, data: Mapping[str, Any]) -> ConnectionRecord:
        self._maybe_fail()
        nickname = str(data["nickname"])
        if nickname.lower() in (r.lower() for r in self._records):
            raise CoreError(
                ErrorCode.CONNECTION_ALREADY_EXISTS,
                f"connection '{nickname}' already exists",
            )
        before = self._build_snapshot()
        record = ConnectionRecord(
            id=str(data["nickname"]),
            nickname=str(data["nickname"]),
            hostname=str(data.get("hostname") or ""),
            username=str(data.get("username") or ""),
            port=int(data.get("port") or 22),
            protocol=str(data.get("protocol") or "ssh"),
            data=dict(data),
        )
        self._records[record.id] = record
        self._bump()
        self._notify(before)
        return record

    def update_connection(
        self,
        connection_id: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        self._maybe_fail()
        self.last_update = dict(data)
        before = self._build_snapshot()
        existing = self._records[connection_id]
        payload = dict(existing.data)
        payload.update(data)
        new_id = str(data.get("nickname") or connection_id)
        payload["nickname"] = new_id
        updated = ConnectionRecord(
            id=new_id,
            nickname=new_id,
            hostname=str(payload.get("hostname") or existing.hostname),
            username=str(payload.get("username") or existing.username),
            port=int(payload.get("port") or existing.port),
            protocol=str(payload.get("protocol") or existing.protocol),
            data=payload,
        )
        self._records.pop(connection_id, None)
        self._records[updated.id] = updated
        self._bump()
        self._notify(before)
        return updated

    def duplicate_connection(self, connection_id: str) -> ConnectionRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        existing = self._records[connection_id]
        record = ConnectionRecord(
            id=f"{existing.id} copy",
            nickname=f"{existing.id} copy",
            hostname=existing.hostname,
            username=existing.username,
            port=existing.port,
            protocol=existing.protocol,
            data=dict(existing.data),
        )
        self._records[record.id] = record
        self._bump()
        self._notify(before)
        return record

    def delete_connection(self, connection_id: str) -> None:
        self._maybe_fail()
        before = self._build_snapshot()
        self._records.pop(connection_id, None)
        self._bump()
        self._notify(before)

    def split_connection(
        self,
        connection_id: str,
        original_host_token: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        record = ConnectionRecord(
            id=str(data["nickname"]),
            nickname=str(data["nickname"]),
            hostname=str(data.get("hostname") or ""),
            username=str(data.get("username") or ""),
            port=int(data.get("port") or 22),
            protocol="ssh",
            data=dict(data),
        )
        self._records[record.id] = record
        self._bump()
        self._notify(before)
        return record

    # -- groups --------------------------------------------------------------

    def create_group(
        self, name: str, *, parent_id: Optional[str] = None, color: str = ""
    ) -> GroupRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        group_id = "".join(ch for ch in name.lower() if ch.isalnum())
        record = GroupRecord(
            id=group_id,
            name=name,
            parent_id=parent_id,
            color=color,
        )
        self._groups[group_id] = record
        self._bump()
        self._notify(before)
        return record

    def rename_group(self, group_id: str, new_name: str) -> GroupRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        self._groups[group_id].name = new_name
        self._bump()
        self._notify(before)
        return self._groups[group_id]

    def delete_group(self, group_id: str) -> None:
        self._maybe_fail()
        before = self._build_snapshot()
        self._groups.pop(group_id, None)
        self._bump()
        self._notify(before)

    def set_group_color(self, group_id: str, color: str) -> GroupRecord:
        self._groups[group_id].color = color
        self._bump()
        return self._groups[group_id]

    def place_group(
        self,
        group_id: str,
        parent_id: Optional[str],
        index: int,
        *,
        expected_generation: Optional[int] = None,
    ) -> GroupRecord:
        self._groups[group_id].parent_id = parent_id
        self._groups[group_id].order = index
        self._bump()
        return self._groups[group_id]

    def assign_connection_to_group(
        self, connection_id: str, group_id: Optional[str]
    ) -> ConnectionRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        if group_id is not None and connection_id not in self._groups.get(group_id, GroupRecord(id=group_id, name="")).connection_ids:
            self._groups[group_id].connection_ids.append(connection_id)
        self._bump()
        self._notify(before)
        return self._records[connection_id]

    def copy_connection_to_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        group = self._groups[group_id]
        if connection_id not in group.connection_ids:
            group.connection_ids.append(connection_id)
        self._bump()
        self._notify(before)
        return self._records[connection_id]

    def remove_connection_from_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        self._maybe_fail()
        before = self._build_snapshot()
        group = self._groups[group_id]
        if connection_id in group.connection_ids:
            group.connection_ids.remove(connection_id)
        self._bump()
        self._notify(before)
        return self._records[connection_id]

    def reorder_connection(
        self,
        connection_id: str,
        target_connection_id: str,
        group_id: Optional[str],
        position: str,
    ) -> None:
        self._maybe_fail()
        before = self._build_snapshot()
        self._bump()
        self._notify(before)

    # -- metadata / tags ----------------------------------------------------

    def update_connection_metadata(
        self, connection_id: str, values: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self._maybe_fail()
        self._bump()
        return dict(values)

    def rename_tag(self, old_tag: str, new_tag: str) -> int:
        self._maybe_fail()
        self._bump()
        return 0

    # -- helpers ------------------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected repository failure")

    def _summary(self, record: ConnectionRecord) -> ConnectionSummary:
        return ConnectionSummary(
            id=ConnectionId(record.id),
            nickname=record.nickname,
            host=record.host or record.id,
            hostname=record.hostname,
            username=record.username,
            port=record.port,
            protocol=record.protocol,
            health=ConnectionHealth.UNKNOWN,
            groups=tuple(
                GroupReference(id=gid, name=self._groups[gid].name)
                for gid in self._group_ids_of(record.id)
            ),
        )

    def _group_ids_of(self, connection_id: str) -> List[str]:
        return [
            gid
            for gid, g in self._groups.items()
            if connection_id in g.connection_ids
        ]

    def _build_snapshot(self) -> ConnectionStoreSnapshot:
        connections = tuple(self._summary(r) for r in self._records.values())
        grouped_ids = {
            cid
            for g in self._groups.values()
            for cid in g.connection_ids
        }
        root_ids = tuple(
            ConnectionId(r.id) for r in self._records.values()
            if r.id not in grouped_ids
        )
        group_summaries = tuple(
            GroupSummary(
                id=g.id,
                name=g.name,
                parent_id=g.parent_id,
                order=g.order,
                color=g.color,
                connection_ids=tuple(ConnectionId(cid) for cid in g.connection_ids),
            )
            for g in self._groups.values()
        )
        return ConnectionStoreSnapshot(
            generation=self._generation,
            connections=connections,
            groups=group_summaries,
            root_connection_ids=root_ids,
            metadata=(),
        )

    def _bump(self) -> None:
        self._generation += 1

    def _notify(self, before: ConnectionStoreSnapshot) -> None:
        after = self._build_snapshot()
        change = RepositoryChange(before=before, after=after)
        for listener in list(self._listeners):
            listener(change)


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------


def _record(record_id: str = "demo", **kwargs: Any) -> ConnectionRecord:
    """Quick ``ConnectionRecord`` factory with sensible defaults."""
    return ConnectionRecord(
        id=record_id,
        nickname=kwargs.pop("nickname", record_id),
        hostname=kwargs.pop("hostname", "example.com"),
        username=kwargs.pop("username", "alice"),
        port=kwargs.pop("port", 22),
        protocol=kwargs.pop("protocol", "ssh"),
        data=kwargs.pop("data", {}),
        source=kwargs.pop("source", ""),
        generation=kwargs.pop("generation", 0),
        host=kwargs.pop("host", record_id),
        aliases=kwargs.pop("aliases", ()),
    )


def make_test_repository(
    *records: ConnectionRecord,
) -> FakeConnectionRepository:
    """Create a repository pre-filled with the given records.

    With no arguments the repository contains a single default ``demo`` record.
    """
    return FakeConnectionRepository(
        list(records) if records else [_record()]
    )


def make_test_connection_service(
    repository: Optional[FakeConnectionRepository] = None,
    *,
    launch_provider: Any = None,
    secret_provider: Any = None,
    client_name: str = "test",
    **kwargs: Any,
) -> "ConnectionApplicationService":
    """Create a ``ConnectionApplicationService`` backed by a fake repository.

    With no arguments the service is backed by a repository containing a
    single default ``demo`` record.
    """
    from sshpilot.core.connection_application_service import (  # noqa: E402
        ConnectionApplicationService,
    )

    if repository is None:
        repository = make_test_repository()
    return ConnectionApplicationService(
        repository,
        launch_provider=launch_provider,
        secret_provider=secret_provider,
        client_name=client_name,
        **kwargs,
    )
