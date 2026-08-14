"""ConnectionApplicationService over the real headless repository.

These tests prove the production wiring: the application service accepts a
``ConnectionRepository`` (a ``ConnectionRepositoryProtocol``) and routes every
read, mutation, and event through it. No ``ConnectionManager`` or
``GroupManager`` is involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connection_application_service import (  # noqa: E402
    ConnectionApplicationService,
)
from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepository,
    ConnectionRepositoryProtocol,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.api.models.connections import (  # noqa: E402
    ConnectionDetails,
    ConnectionEditorDetails,
    ConnectionId,
    ConnectionMutationResult,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    SplitConnectionRequest,
    UpdateConnectionRequest,
)
from sshpilot.api.events import EventType  # noqa: E402


def _repo(tmp_path, ssh_text: str = ""):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    return repo, root


def _service(repo):
    return ConnectionApplicationService(repo, client_name="sshpilotd")


def _create_req(nickname, hostname="example.net", **kwargs):
    return CreateConnectionRequest(
        nickname=nickname,
        hostname=hostname,
        username=kwargs.pop("username", "alice"),
        port=kwargs.pop("port", 22),
        protocol=kwargs.pop("protocol", "ssh"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_repository_satisfies_protocol(tmp_path):
    repo, _root = _repo(tmp_path)
    assert isinstance(repo, ConnectionRepositoryProtocol)


def test_service_rejects_none_repository():
    with pytest.raises(ValueError):
        ConnectionApplicationService(None, client_name="sshpilotd")


# ---------------------------------------------------------------------------
# Reads route through the repository
# ---------------------------------------------------------------------------


def test_list_connections_returns_public_summaries(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n    User bob\n")
    service = _service(repo)
    summaries = service.list_connections()
    assert [s.id for s in summaries] == ["web"]
    summary = summaries[0]
    assert summary.nickname == "web"
    assert summary.hostname == "example.com"
    assert summary.username == "bob"
    # Secret-free public shape.
    assert not hasattr(summary, "password")
    assert not hasattr(summary, "key_passphrase")


def test_get_connection_returns_details(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    details = service.get_connection(ConnectionId("web"))
    assert isinstance(details, ConnectionDetails)
    assert details.hostname == "example.com"


def test_get_connection_editor_returns_editor_details(tmp_path):
    repo, _root = _repo(
        tmp_path,
        "Host web\n    HostName example.com\n    IdentityFile ~/.ssh/id_ed25519\n",
    )
    service = _service(repo)
    editor = service.get_connection_editor(ConnectionId("web"))
    assert isinstance(editor, ConnectionEditorDetails)
    assert len(editor.identity_files) == 1
    assert editor.identity_files[0].endswith(".ssh/id_ed25519")


def test_get_connection_editor_preserves_display_name(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.set_display_name("web", "Production Web")

    editor = _service(repo).get_connection_editor(ConnectionId("web"))

    assert editor.display_name == "Production Web"


def test_get_missing_connection_raises_not_found(tmp_path):
    repo, _root = _repo(tmp_path)
    service = _service(repo)
    with pytest.raises(Exception) as exc:
        service.get_connection(ConnectionId("missing"))
    assert getattr(exc.value, "code", None) is not None


def test_snapshot_connection_store(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    snap = service.snapshot_connection_store()
    assert [c.id for c in snap.connections] == ["web"]
    assert snap.generation == 0


# ---------------------------------------------------------------------------
# Mutations route through the repository
# ---------------------------------------------------------------------------


def test_create_connection_routes(tmp_path):
    repo, root = _repo(tmp_path, "")
    service = _service(repo)
    result = service.create_connection(_create_req("new"))
    assert isinstance(result, ConnectionMutationResult)
    assert result.nickname == "new"
    assert "Host new" in root.read_text()


def test_update_connection_routes_and_bumps_generation(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    result = service.update_connection(
        ConnectionId("web"),
        UpdateConnectionRequest(
            hostname="example.org",
            username="carol",
            port=2222,
        ),
    )
    assert result.connection_id == "web"
    details = service.get_connection(ConnectionId("web"))
    assert details.hostname == "example.org"
    assert details.username == "carol"
    assert details.port == 2222


def test_stale_editor_rejected(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    with pytest.raises(Exception) as exc:
        service.update_connection(
            ConnectionId("web"),
            UpdateConnectionRequest(
                hostname="example.org",
                expected_generation=99,
            ),
        )
    code = getattr(exc.value, "code", None)
    assert code is not None and "STALE" in str(code).upper()


def test_duplicate_connection_routes(tmp_path):
    repo, root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    result = service.duplicate_connection(ConnectionId("web"))
    assert result.connection_id == "web-Copy"
    assert "Host web-Copy" in root.read_text()


def test_delete_connection_routes(tmp_path):
    repo, root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    service.delete_connection(DeleteConnectionRequest(connection_id="web"))
    assert "Host web" not in root.read_text()
    assert [c.id for c in service.list_connections()] == []


def test_split_connection_routes(tmp_path):
    repo, root = _repo(
        tmp_path, "Host db jump\n    HostName=db.internal\n    User dbuser\n"
    )
    service = _service(repo)
    result = service.split_connection(
        SplitConnectionRequest(
            connection_id="jump",
            original_host_token="jump",
            source_config_path=str(root),
            nickname="jump2",
            hostname="jump.example",
            config_patch={},
        )
    )
    assert result.connection_id == "jump2"
    text = root.read_text()
    assert "Host db" in text
    assert "Host jump2" in text


# ---------------------------------------------------------------------------
# Group and metadata mutations route
# ---------------------------------------------------------------------------


def test_group_mutations_route(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    group_id = service.create_group_rpc("Production")
    assert group_id is not None
    assert service.set_group_color_rpc(group_id, "#ff0000") is True
    assert service.copy_connection_to_group_rpc("web", group_id) is True
    snap = service.snapshot_connection_store()
    assert [g.name for g in snap.groups] == ["Production"]
    web = next(c for c in snap.connections if c.id == "web")
    assert [g.id for g in web.groups] == [group_id]


def test_metadata_mutations_route(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    assert (
        service.update_connection_metadata(
            ConnectionId("web"), {"tags": ["prod"], "pinned": True}
        )
        is True
    )
    snap = service.snapshot_connection_store()
    meta = next(m for m in snap.metadata if m.connection_id == "web")
    assert meta.values["tags"] == ("prod",)
    assert meta.values["pinned"] is True


def test_rename_tag_routes(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    service.update_connection_metadata(ConnectionId("web"), {"tags": ["old"]})
    assert service.rename_tag_rpc("old", "new") == 1
    snap = service.snapshot_connection_store()
    meta = next(m for m in snap.metadata if m.connection_id == "web")
    assert meta.values["tags"] == ("new",)


# ---------------------------------------------------------------------------
# Events: one coherent set per committed change, deterministic order
# ---------------------------------------------------------------------------


def test_create_publishes_created_then_store_changed(tmp_path):
    repo, _root = _repo(tmp_path, "")
    service = _service(repo)
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    service.create_connection(_create_req("new"))
    assert seen.count(EventType.CONNECTION_CREATED) == 1
    assert seen.count(EventType.CONNECTION_STORE_CHANGED) == 1
    assert seen.index(EventType.CONNECTION_CREATED) < seen.index(
        EventType.CONNECTION_STORE_CHANGED
    )


def test_update_publishes_updated_then_store_changed(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    service.update_connection(
        ConnectionId("web"),
        UpdateConnectionRequest(hostname="example.org"),
    )
    assert seen.count(EventType.CONNECTION_UPDATED) == 1
    assert seen.count(EventType.CONNECTION_STORE_CHANGED) == 1
    assert seen.index(EventType.CONNECTION_UPDATED) < seen.index(
        EventType.CONNECTION_STORE_CHANGED
    )


def test_delete_publishes_deleted_then_store_changed(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    service.delete_connection(DeleteConnectionRequest(connection_id="web"))
    assert seen.count(EventType.CONNECTION_DELETED) == 1
    assert seen.count(EventType.CONNECTION_STORE_CHANGED) == 1
    assert seen.index(EventType.CONNECTION_DELETED) < seen.index(
        EventType.CONNECTION_STORE_CHANGED
    )


def test_pure_group_change_publishes_only_store_changed(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    service.create_group_rpc("Prod")
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    # Renaming a group changes no public ConnectionSummary.
    assert service.rename_group_rpc("prod", "Production") is True
    assert seen == [EventType.CONNECTION_STORE_CHANGED]


def test_no_events_on_failed_mutation(tmp_path):
    repo, _root = _repo(tmp_path, "Host web\n    HostName example.com\n")
    service = _service(repo)
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    with pytest.raises(Exception):
        service.create_connection(_create_req("web"))  # duplicate
    assert seen == []


def test_exactly_one_store_changed_per_successful_mutation(tmp_path):
    repo, _root = _repo(tmp_path, "")
    service = _service(repo)
    seen = []
    service.subscribe_events(lambda event: seen.append(event.type))
    service.create_connection(_create_req("one"))
    service.create_connection(_create_req("two"))
    assert seen.count(EventType.CONNECTION_STORE_CHANGED) == 2
    assert seen.count(EventType.CONNECTION_CREATED) == 2


def test_store_changed_payload_is_full_snapshot(tmp_path):
    repo, _root = _repo(tmp_path, "")
    service = _service(repo)
    payloads = []
    service.subscribe_events(lambda event: payloads.append(event.payload))
    service.create_connection(_create_req("new"))
    snapshot = payloads[-1]
    assert [c.id for c in snapshot.connections] == ["new"]
    assert snapshot.generation == 1
