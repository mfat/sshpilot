"""ConnectionApplicationService behavior over a fake repository.

These tests exercise the service's own behavior (DTO conversion, capability
gating, launch/secret provider delegation, canonical unsupported methods) with
a minimal in-memory ``ConnectionRepositoryProtocol`` fake. No filesystem, no
``ConnectionManager``, no ``GroupManager``.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connection_application_service import (  # noqa: E402
    ConnectionApplicationService,
)
from sshpilot.core.connections.models import ConnectionRecord, GroupRecord  # noqa: E402
from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepositoryProtocol,
    RepositoryChange,
)
from sshpilot.api.models.connection_store import (  # noqa: E402
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
    GroupSummary,
)
from sshpilot.api.models.connections import (  # noqa: E402
    ConnectionHealth,
    ConnectionId,
    ConnectionSummary,
    GroupReference,
    UpdateConnectionRequest,
)
from sshpilot.api.capabilities import Capability  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402


class FakeRepository(ConnectionRepositoryProtocol):
    """In-memory repository fake: records calls, returns canned records."""

    def __init__(self, records: Optional[List[ConnectionRecord]] = None) -> None:
        self._records: Dict[str, ConnectionRecord] = {}
        for record in records or []:
            self._records[record.id] = record
        self._groups: Dict[str, GroupRecord] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._generation = 0
        self.calls: List[str] = []
        self.listeners = []
        self.fail_next = False
        self.fail_update_with: Optional[CoreError] = None

    # -- protocol surface ---------------------------------------------------

    def snapshot(self) -> ConnectionStoreSnapshot:
        self.calls.append("snapshot")
        group_summaries = tuple(
            GroupSummary(
                id=g.id,
                name=g.name,
                parent_id=g.parent_id,
                order=g.order,
                color=g.color,
                connection_ids=tuple(g.connection_ids),
            )
            for g in self._groups.values()
        )
        connections = tuple(self._summary(r) for r in self._records.values())
        root = tuple(
            r.id for r in self._records.values() if not self._group_ids_of(r.id)
        )
        metadata = tuple(
            ConnectionMetadataSummary(
                connection_id=ConnectionId(cid), values=values
            )
            for cid, values in self._metadata.items()
            if cid in self._records
        )
        return ConnectionStoreSnapshot(
            generation=self._generation,
            connections=connections,
            groups=group_summaries,
            root_connection_ids=root,
            metadata=metadata,
        )

    def list_records(self):
        self.calls.append("list_records")
        return tuple(self._records.values())

    def get_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        self.calls.append("get_record")
        return self._records.get(connection_id)

    def get_editor_record(self, connection_id: str) -> Optional[ConnectionRecord]:
        self.calls.append("get_editor_record")
        return self._records.get(connection_id)

    def discover_paths(self) -> frozenset:
        self.calls.append("discover_paths")
        return frozenset()

    def reload(self) -> ConnectionStoreSnapshot:
        self.calls.append("reload")
        return self.snapshot()

    def add_listener(self, callback) -> None:
        self.listeners.append(callback)

    def remove_listener(self, callback) -> None:
        if callback in self.listeners:
            self.listeners.remove(callback)

    # -- mutations ----------------------------------------------------------

    def create_connection(self, data: Mapping[str, Any]) -> ConnectionRecord:
        self.calls.append("create_connection")
        self._mutate()
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
        return record

    def update_connection(
        self,
        connection_id: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        self.calls.append("update_connection")
        if self.fail_update_with is not None:
            error = self.fail_update_with
            self.fail_update_with = None
            raise error
        self._mutate()
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
        return updated

    def duplicate_connection(self, connection_id: str) -> ConnectionRecord:
        self.calls.append("duplicate_connection")
        self._mutate()
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
        return record

    def delete_connection(self, connection_id: str) -> None:
        self.calls.append("delete_connection")
        self._mutate()
        self._records.pop(connection_id, None)
        self._metadata.pop(connection_id, None)
        self._bump()

    def split_connection(
        self,
        connection_id: str,
        original_host_token: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> ConnectionRecord:
        self.calls.append("split_connection")
        self._mutate()
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
        return record

    # -- groups -------------------------------------------------------------

    def create_group(
        self, name: str, *, parent_id: Optional[str] = None, color: str = ""
    ) -> GroupRecord:
        self.calls.append("create_group")
        self._mutate()
        group_id = "".join(ch for ch in name.lower() if ch.isalnum())
        record = GroupRecord(
            id=group_id,
            name=name,
            parent_id=parent_id,
            color=color,
        )
        self._groups[group_id] = record
        self._bump()
        return record

    def rename_group(self, group_id: str, new_name: str) -> GroupRecord:
        self.calls.append("rename_group")
        self._mutate()
        self._groups[group_id].name = new_name
        self._bump()
        return self._groups[group_id]

    def delete_group(self, group_id: str) -> None:
        self.calls.append("delete_group")
        self._mutate()
        self._groups.pop(group_id, None)
        self._bump()

    def set_group_color(self, group_id: str, color: str) -> GroupRecord:
        self.calls.append("set_group_color")
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
        self.calls.append("place_group")
        self._groups[group_id].parent_id = parent_id
        self._groups[group_id].order = index
        self._bump()
        return self._groups[group_id]

    def assign_connection_to_group(
        self, connection_id: str, group_id: Optional[str]
    ) -> ConnectionRecord:
        self.calls.append("assign_connection_to_group")
        self._bump()
        return self._records[connection_id]

    def copy_connection_to_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        self.calls.append("copy_connection_to_group")
        group = self._groups[group_id]
        if connection_id not in group.connection_ids:
            group.connection_ids.append(connection_id)
        self._bump()
        return self._records[connection_id]

    def remove_connection_from_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        self.calls.append("remove_connection_from_group")
        group = self._groups[group_id]
        if connection_id in group.connection_ids:
            group.connection_ids.remove(connection_id)
        self._bump()
        return self._records[connection_id]

    def reorder_connection(
        self,
        connection_id: str,
        target_connection_id: str,
        group_id: Optional[str],
        position: str,
    ) -> None:
        self.calls.append("reorder_connection")
        self._bump()

    # -- metadata -----------------------------------------------------------

    def update_connection_metadata(
        self, connection_id: str, values: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append("update_connection_metadata")
        self._mutate()
        current = dict(self._metadata.get(connection_id, {}))
        current.update(dict(values))
        if current:
            self._metadata[connection_id] = current
        self._bump()
        return dict(self._metadata.get(connection_id, {}))

    def set_display_name(self, connection_id: str, display_name: str) -> ConnectionRecord:
        self.calls.append("set_display_name")
        self._mutate()
        record = self._records[connection_id]
        payload = dict(record.data)
        payload["display_name"] = display_name
        updated = ConnectionRecord(
            id=record.id,
            nickname=record.nickname,
            hostname=record.hostname,
            username=record.username,
            port=record.port,
            protocol=record.protocol,
            data=payload,
            source=record.source,
            generation=record.generation,
            host=record.host,
            aliases=record.aliases,
        )
        self._records[connection_id] = updated
        self._bump()
        return updated

    def rename_tag(self, old_tag: str, new_tag: str) -> None:
        self.calls.append("rename_tag")
        for values in self._metadata.values():
            tags = values.get("tags")
            if isinstance(tags, list):
                values["tags"] = [
                    new_tag if str(t) == old_tag else t for t in tags
                ]
        self._bump()

    # -- helpers ------------------------------------------------------------

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
            display_name=str(record.data.get("display_name") or record.nickname),
        )

    def _group_ids_of(self, connection_id: str) -> List[str]:
        return [
            gid
            for gid, g in self._groups.items()
            if connection_id in g.connection_ids
        ]

    def _mutate(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected repository failure")

    def _bump(self) -> None:
        self._generation += 1
        after = self.snapshot()
        for listener in list(self.listeners):
            listener(RepositoryChange(before=after, after=after))


def _record(record_id="web", **kwargs) -> ConnectionRecord:
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


# ---------------------------------------------------------------------------
# DTO conversion
# ---------------------------------------------------------------------------


def test_record_converts_to_summary():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    summary = service.list_connections()[0]
    assert summary.id == "web"
    assert summary.nickname == "web"
    assert summary.hostname == "example.com"
    assert summary.username == "alice"
    assert summary.port == 22


def test_display_name_update_is_additive_and_keeps_alias_id():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    result = service.update_connection(
        ConnectionId("web"),
        UpdateConnectionRequest(display_name="Production Server / تهران"),
    )
    assert result.connection_id == "web"
    assert result.display_name == "Production Server / تهران"
    assert repo.snapshot().connections[0].id == "web"


def test_record_converts_to_details_and_editor():
    repo = FakeRepository(
        [
            _record(
                data={
                    "identity_files": ["~/.ssh/id_ed25519"],
                    "auth_method": 1,
                    "forwarding_rules": [],
                }
            )
        ]
    )
    service = ConnectionApplicationService(repo, client_name="test")
    details = service.get_connection(ConnectionId("web"))
    assert details.authentication_method.value == "password"
    assert details.identity_configured is True
    editor = service.get_connection_editor(ConnectionId("web"))
    assert editor.identity_files == ("~/.ssh/id_ed25519",)
    assert editor.generation == 0


def test_missing_record_raises_not_found():
    repo = FakeRepository([])
    service = ConnectionApplicationService(repo, client_name="test")
    with pytest.raises(Exception) as exc:
        service.get_connection(ConnectionId("missing"))
    assert "not exist" in str(exc.value).lower()


def test_update_failure_logs_core_diagnostics_and_keeps_safe_api_error(caplog):
    repo = FakeRepository([_record()])
    repo.fail_update_with = CoreError(
        ErrorCode.CONNECTION_STATE_IO_ERROR,
        "The connection configuration could not be saved",
        diagnostic_category="persistence",
        diagnostic_reason="state file write failed",
    )
    service = ConnectionApplicationService(repo, client_name="test")

    with caplog.at_level(logging.WARNING, logger="sshpilot.core.connection_application_service"):
        with pytest.raises(Exception) as exc_info:
            service.update_connection(
                ConnectionId("web"),
                UpdateConnectionRequest(hostname="updated.example"),
            )

    assert getattr(exc_info.value, "code", None) is not None
    assert "The connection state could not be stored" in str(exc_info.value)
    assert "CONNECTION_STATE_IO_ERROR" in caplog.text
    assert "persistence" in caplog.text
    assert "state file write failed" in caplog.text


# ---------------------------------------------------------------------------
# Provider delegation
# ---------------------------------------------------------------------------


def test_launch_provider_delegation():
    class LaunchProvider:
        def prepare_terminal_launch(
            self, connection_id, *, interaction_policy="none", remote_command=None, force_tty=False
        ):
            return ("ssh", ["-p", "22", "web@example.com"])

    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(
        repo, launch_provider=LaunchProvider(), client_name="test"
    )
    argv = service.prepare_daemon_terminal_launch(ConnectionId("web"))
    assert argv == ("ssh", ["-p", "22", "web@example.com"])


def test_missing_launch_provider_raises_startup_error():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    with pytest.raises(Exception) as exc:
        service.prepare_daemon_terminal_launch(ConnectionId("web"))
    assert "could not be prepared" in str(exc.value).lower()


def test_secret_provider_delegation():
    class SecretProvider:
        def lookup_connection_password(self, connection_id):
            return "hunter2"

        def has_connection_password(self, connection_id):
            return True

    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(
        repo, secret_provider=SecretProvider(), client_name="test"
    )
    assert service.lookup_daemon_password(ConnectionId("web")) == "hunter2"
    assert service.has_connection_password(ConnectionId("web")) is True
    assert service.reveal_connection_password(ConnectionId("web")) == bytearray(b"hunter2")


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


def test_list_connections_requires_read_capability():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    capabilities = service.get_capabilities()
    assert capabilities.supports(Capability.CONNECTIONS_READ)
    assert capabilities.supports(Capability.CONNECTIONS_WRITE)
    assert capabilities.supports(Capability.CONNECTIONS_GROUPS)


def test_canonical_unsupported_key_methods():
    repo = FakeRepository([])
    service = ConnectionApplicationService(repo, client_name="test")
    for method in (service.list_keys, service.read_public_key, service.generate_key):
        with pytest.raises(Exception) as exc:
            method(None)
        assert "not supported" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Thread / lifecycle behavior
# ---------------------------------------------------------------------------


def test_cross_thread_command_rejected_by_default():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    errors = []

    def other_thread():
        try:
            service.list_connections()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1
    assert "owner thread" in str(errors[0]).lower()


def test_enable_serialized_command_threads_allows_cross_thread():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    service.enable_serialized_command_threads()
    results = []
    thread = threading.Thread(target=lambda: results.append(service.list_connections()))
    thread.start()
    thread.join()
    assert len(results) == 1
    assert [c.id for c in results[0]] == ["web"]


def test_close_stops_events():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    service.close()
    with pytest.raises(Exception):
        service.subscribe_events(lambda *a, **kw: None)


def test_subscribe_then_close_detaches_listener():
    repo = FakeRepository([_record()])
    service = ConnectionApplicationService(repo, client_name="test")
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    service.close()
    assert repo.listeners == []
    assert seen == []
