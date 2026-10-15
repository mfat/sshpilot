"""Batch deletion: one transaction, one SSH config write, one store event."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.api import ErrorCode as ApiErrorCode, SshPilotError  # noqa: E402
from sshpilot.api.models import DeleteConnectionsRequest  # noqa: E402
from sshpilot.core.connection_application_service import (  # noqa: E402
    ConnectionApplicationService,
)
from sshpilot.core.connections import ssh_config_store as store_module  # noqa: E402
from sshpilot.core.connections.repository import ConnectionRepository  # noqa: E402
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402

FIVE_HOSTS = "".join(
    f"Host h{i}\n    HostName h{i}.example\n\n" for i in range(5)
)


def _repo(tmp_path, ssh_text: str = FIVE_HOSTS):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    changes = []
    repo.add_listener(changes.append)
    return repo, root, tmp_path / "connections.json", changes


def _ids(repo):
    return [item.id for item in repo.snapshot().connections]


def _count_full_parses(monkeypatch):
    calls = []
    real = store_module.load_ssh_configuration

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(store_module, "load_ssh_configuration", counting)
    return calls


def test_batch_delete_removes_every_host_with_one_write_and_one_event(
    tmp_path, monkeypatch
):
    repo, root, _state, changes = _repo(tmp_path)
    parses = _count_full_parses(monkeypatch)
    writes = []
    real_write = store_module._atomic_write_text
    monkeypatch.setattr(
        store_module,
        "_atomic_write_text",
        lambda path, *a, **k: (writes.append(Path(path)), real_write(path, *a, **k))[1],
    )

    removed = repo.delete_connections(["h1", "h3", "h4"])

    assert removed == ("h1", "h3", "h4")
    assert _ids(repo) == ["h0", "h2"]
    text = root.read_text()
    assert "Host h1" not in text and "Host h3" not in text and "Host h4" not in text
    assert "Host h0" in text and "Host h2" in text
    assert writes == [root]
    assert len(changes) == 1
    # Same full-parse budget as deleting one host, whatever the batch size.
    assert len(parses) <= 4


def test_single_delete_parses_the_config_at_most_four_times(tmp_path, monkeypatch):
    repo, _root, _state, changes = _repo(tmp_path)
    parses = _count_full_parses(monkeypatch)

    repo.delete_connection("h2")

    assert "h2" not in _ids(repo)
    assert len(changes) == 1
    assert len(parses) <= 4


def test_batch_delete_of_aliases_sharing_a_host_line_keeps_the_others(tmp_path):
    repo, root, _state, _changes = _repo(
        tmp_path, "Host db jump bastion\n    HostName db.internal\n"
    )

    repo.delete_connections(["jump", "bastion"])

    assert _ids(repo) == ["db"]
    assert "Host db\n" in root.read_text()


def test_batch_delete_drops_group_membership_and_metadata(tmp_path):
    repo, _root, _state, _changes = _repo(tmp_path)
    group = repo._service.create_group("Prod").id
    repo._service.copy_connection_to_group("h1", group)
    repo._metadata["h1"] = {"pinned": True}

    repo.delete_connections(["h1", "h2"])

    snapshot = repo.snapshot()
    assert [item.id for item in snapshot.connections] == ["h0", "h3", "h4"]
    assert all(item.connection_id != "h1" for item in snapshot.metadata)
    (prod,) = snapshot.groups
    assert prod.connection_ids == ()
    restarted, _root, _state, _changes = _repo(tmp_path, ssh_text="")
    assert [item.id for item in restarted.snapshot().connections] == [
        "h0", "h3", "h4",
    ]
    assert restarted.snapshot().metadata == ()


def test_batch_delete_mixes_ssh_and_non_ssh_connections(tmp_path):
    repo, root, _state, changes = _repo(tmp_path)
    repo.create_connection(
        {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
    )
    changes.clear()

    repo.delete_connections(["tel", "h0"])

    assert _ids(repo) == ["h1", "h2", "h3", "h4"]
    assert "Host h0" not in root.read_text()
    assert len(changes) == 1


def test_batch_delete_across_included_files_is_one_event(tmp_path):
    fragment = tmp_path / "fragment.conf"
    fragment.write_text("Host inc1\n    HostName inc1.example\n\nHost inc2\n    HostName inc2.example\n")
    repo, root, _state, changes = _repo(
        tmp_path, "Include fragment.conf\n\n" + FIVE_HOSTS
    )

    repo.delete_connections(["inc1", "h0"])

    assert set(_ids(repo)) == {"inc2", "h1", "h2", "h3", "h4"}
    assert "inc1" not in fragment.read_text()
    assert "Host h0" not in root.read_text()
    assert len(changes) == 1


def test_batch_delete_with_an_unknown_id_deletes_nothing(tmp_path):
    repo, root, state, changes = _repo(tmp_path)
    before_root = root.read_bytes()
    before_state = state.read_bytes()

    with pytest.raises(CoreError) as error:
        repo.delete_connections(["h1", "missing", "h2"])

    assert error.value.code is ErrorCode.CONNECTION_NOT_FOUND
    assert root.read_bytes() == before_root
    assert state.read_bytes() == before_state
    assert _ids(repo) == ["h0", "h1", "h2", "h3", "h4"]
    assert changes == []


def test_batch_delete_rolls_back_every_file_when_the_state_write_fails(
    tmp_path, monkeypatch
):
    repo, root, state, changes = _repo(tmp_path)
    repo.create_connection(
        {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
    )
    changes.clear()
    before_root = root.read_bytes()
    before_state = state.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_identity_state_v2",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.delete_connections(["h1", "h3", "tel"])
    monkeypatch.undo()

    assert root.read_bytes() == before_root
    assert state.read_bytes() == before_state
    assert not state.with_name(state.name + ".pending").exists()
    assert _ids(repo) == ["h0", "h1", "h2", "h3", "h4", "tel"]
    assert changes == []


def test_batch_delete_rolls_back_the_first_file_when_a_later_file_fails(
    tmp_path, monkeypatch
):
    fragment = tmp_path / "fragment.conf"
    fragment.write_text("Host inc1\n    HostName inc1.example\n")
    repo, root, _state, changes = _repo(
        tmp_path, "Include fragment.conf\n\n" + FIVE_HOSTS
    )
    before = {path: path.read_bytes() for path in (root, fragment)}
    real_prepare = SshConfigStore.prepare_delete_many
    prepared = []

    def fail_second(self, ids):
        prepared.append(tuple(ids))
        if len(prepared) == 2:
            raise CoreError(ErrorCode.STALE_CONNECTION_STATE, "injected")
        return real_prepare(self, ids)

    monkeypatch.setattr(SshConfigStore, "prepare_delete_many", fail_second)
    with pytest.raises(CoreError):
        repo.delete_connections(["inc1", "h0"])
    monkeypatch.undo()

    assert len(prepared) == 2
    assert {path: path.read_bytes() for path in (root, fragment)} == before
    assert set(_ids(repo)) == {"inc1", "h0", "h1", "h2", "h3", "h4"}
    assert changes == []


def test_batch_delete_rejects_an_empty_request(tmp_path):
    repo, _root, _state, _changes = _repo(tmp_path)
    with pytest.raises(CoreError) as error:
        repo.delete_connections([])
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_application_service_batch_delete(tmp_path):
    repo, _root, _state, _changes = _repo(tmp_path)
    application = ConnectionApplicationService(repo, client_name="test")

    result = application.delete_connections(
        DeleteConnectionsRequest(connection_ids=("h0", "h4"))
    )

    assert result.connection_ids == ("h0", "h4")
    assert _ids(repo) == ["h1", "h2", "h3"]


def test_application_service_batch_delete_names_the_unknown_id(tmp_path):
    repo, _root, _state, _changes = _repo(tmp_path)
    application = ConnectionApplicationService(repo, client_name="test")

    with pytest.raises(SshPilotError) as error:
        application.delete_connections(
            DeleteConnectionsRequest(connection_ids=("h0", "nope"))
        )

    assert error.value.code is ApiErrorCode.CONNECTION_NOT_FOUND
    assert _ids(repo) == ["h0", "h1", "h2", "h3", "h4"]


def test_delete_connections_request_validates_ids():
    with pytest.raises(ValueError):
        DeleteConnectionsRequest(connection_ids=())
    with pytest.raises(ValueError):
        DeleteConnectionsRequest(connection_ids=("a", "a"))
