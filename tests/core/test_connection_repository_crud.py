"""Transactional CRUD tests for the headless connection repository."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepository,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402


def _repo(tmp_path, ssh_text: str = ""):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    ), root, tmp_path / "connections.json"


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _state(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# SSH CRUD
# ---------------------------------------------------------------------------


def test_ssh_create(tmp_path):
    repo, root, state = _repo(tmp_path, "")
    created = repo.create_connection(
        {"nickname": "new", "hostname": "example.net", "username": "u", "protocol": "ssh"}
    )
    assert created.id == "new"
    assert "Host new" in root.read_text()
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["new"]
    assert snap.generation == 1


def test_ssh_create_rolls_back_when_state_write_fails(tmp_path, monkeypatch):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    before_root = root.read_bytes()
    before_state = state.read_bytes() if state.exists() else None
    before_snapshot = repo.snapshot()
    events = []
    repo.add_listener(events.append)

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.create_connection(
            {"nickname": "new", "hostname": "example.net", "protocol": "ssh"}
        )
    assert root.read_bytes() == before_root
    assert (state.read_bytes() if state.exists() else None) == before_state
    assert repo.snapshot() == before_snapshot
    assert events == []


def test_ssh_create_duplicate_rejected(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    with pytest.raises(CoreError) as exc:
        repo.create_connection(
            {"nickname": "web", "hostname": "x", "protocol": "ssh"}
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert repo.snapshot().generation == 0


def test_ssh_update_rewrites_only_target_block(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "# header\nHost web\n    # pinned\n    HostName example.com\n    User alice\n\n"
        "Host other\n    HostName o.example.com\n",
    )
    repo.update_connection(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    text = root.read_text()
    assert "# header" in text
    assert "# pinned" in text
    assert "HostName example.org" in text
    assert "Host other" in text


def test_ssh_rename_migrates_everything(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n    User alice\n")
    # Set up group membership and metadata through the internal store (the
    # public group/metadata ops land in the next repository task).
    gid = repo._service.create_group("Prod").id
    repo._service.copy_connection_to_group("web", gid)
    repo._metadata["web"] = {"pinned": True, "tags": ["a"]}
    repo.update_connection(
        "web",
        {"nickname": "web2", "hostname": "example.com", "username": "alice", "protocol": "ssh"},
        expected_generation=0,
    )
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["web2"]
    assert snap.connections[0].groups[0].id == gid
    assert snap.metadata[0].connection_id == "web2"
    assert "Host web2" in root.read_text()
    assert "Host web\n" not in root.read_text()


def test_ssh_rename_stale_editor_rejected(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.update_connection(
        "web",
        {"nickname": "web", "hostname": "a", "protocol": "ssh"},
        expected_generation=0,
    )
    with pytest.raises(CoreError) as exc:
        repo.update_connection(
            "web",
            {"nickname": "web2", "hostname": "b", "protocol": "ssh"},
            expected_generation=0,  # stale: current is 1
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    # Nothing changed.
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["web"]


def test_ssh_delete_removes_block_and_metadata(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "# header\nHost web\n    HostName example.com\n\nHost other\n    HostName o.example.com\n",
    )
    repo._metadata["web"] = {"pinned": True}
    repo.delete_connection("web")
    text = root.read_text()
    assert "Host web" not in text
    assert "Host other" in text
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["other"]
    assert snap.metadata == ()
    assert "web" not in _state(state)["metadata"]


def test_ssh_delete_one_alias_keeps_block(tmp_path):
    repo, root, state = _repo(tmp_path, "Host db jump\n    HostName=db.internal\n    User dbuser\n")
    repo.delete_connection("jump")
    assert "Host db" in root.read_text()
    assert repo.get_record("db") is not None
    assert repo.get_record("jump") is None


def test_ssh_duplicate_mirrors_group_placement(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n    User alice\n")
    gid = repo._service.create_group("Prod").id
    repo._service.copy_connection_to_group("web", gid)
    dup = repo.duplicate_connection("web")
    assert dup.id != "web"
    snap = repo.snapshot()
    dup_summary = next(c for c in snap.connections if c.id == dup.id)
    assert dup_summary.groups[0].id == gid


def test_ssh_split_one_alias(tmp_path):
    repo, root, state = _repo(tmp_path, "Host db jump\n    HostName=db.internal\n    User dbuser\n")
    result = repo.split_connection(
        "jump",
        "jump",
        {
            "nickname": "jump2",
            "hostname": "jump.example",
            "username": "j",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    assert result.id == "jump2"
    text = root.read_text()
    assert "Host db" in text
    assert "Host jump2" in text
    assert repo.get_record("jump") is None
    assert repo.get_record("jump2") is not None


# ---------------------------------------------------------------------------
# Non-SSH CRUD
# ---------------------------------------------------------------------------


def test_non_ssh_create_persists_to_state_file(tmp_path):
    repo, root, state = _repo(tmp_path)
    created = repo.create_connection(
        {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5", "port": 2323}
    )
    assert created.id == "tel"
    stored = _state(state)
    assert any(d.get("nickname") == "tel" for d in stored["non_ssh_connections"])
    assert "tel" not in root.read_text() if root.exists() else True
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["tel"]


def test_non_ssh_update_and_rename(tmp_path):
    _write_state(
        tmp_path / "connections.json",
        {
            "version": 1,
            "non_ssh_connections": [
                {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
            ],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    repo, root, state = _repo(tmp_path)
    repo.update_connection(
        "tel",
        {"nickname": "tel2", "protocol": "telnet", "hostname": "10.0.0.6"},
        expected_generation=0,
    )
    stored = _state(state)
    names = [d.get("nickname") for d in stored["non_ssh_connections"]]
    assert names == ["tel2"]
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["tel2"]
    assert snap.connections[0].hostname == "10.0.0.6"


def test_non_ssh_delete(tmp_path):
    _write_state(
        tmp_path / "connections.json",
        {
            "version": 1,
            "non_ssh_connections": [
                {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
            ],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    repo, root, state = _repo(tmp_path)
    repo.delete_connection("tel")
    assert _state(state)["non_ssh_connections"] == []
    assert repo.snapshot().connections == ()


def test_non_ssh_duplicate(tmp_path):
    _write_state(
        tmp_path / "connections.json",
        {
            "version": 1,
            "non_ssh_connections": [
                {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
            ],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    repo, root, state = _repo(tmp_path)
    dup = repo.duplicate_connection("tel")
    assert dup.id.startswith("tel-")
    assert len(_state(state)["non_ssh_connections"]) == 2


# ---------------------------------------------------------------------------
# Failure atomicity and events
# ---------------------------------------------------------------------------


def test_no_events_on_failure(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    events = []
    repo.add_listener(lambda change: events.append(change))
    with pytest.raises(CoreError):
        repo.create_connection(
            {"nickname": "web", "hostname": "x", "protocol": "ssh"}
        )
    assert events == []
    assert repo.snapshot().generation == 0


def test_exactly_one_change_per_mutation(tmp_path):
    repo, root, state = _repo(tmp_path)
    events = []
    repo.add_listener(lambda change: events.append(change))
    repo.create_connection(
        {"nickname": "a", "hostname": "a.example", "protocol": "ssh"}
    )
    assert len(events) == 1
    assert events[0].before.generation == 0
    assert events[0].after.generation == 1


def test_write_failure_rolls_back_memory(tmp_path, monkeypatch):
    repo, root, state = _repo(tmp_path)
    import sshpilot.core.connections.ssh_config_store as store_mod

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", _boom)
    with pytest.raises(CoreError):
        repo.create_connection(
            {"nickname": "x", "hostname": "x.example", "protocol": "ssh"}
        )
    monkeypatch.undo()
    assert repo.get_record("x") is None
    assert repo.snapshot().generation == 0


def test_concurrent_writes_serialized(tmp_path):
    repo, root, state = _repo(tmp_path)
    errors = []
    barrier = threading.Barrier(3)

    def writer(i):
        barrier.wait()
        try:
            repo.create_connection(
                {"nickname": f"h{i}", "hostname": f"h{i}.example", "protocol": "ssh"}
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()
    assert not errors
    snap = repo.snapshot()
    assert len(snap.connections) == 8


def test_rename_grouped_connection_without_metadata_survives_reload(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    gid = repo._service.create_group("Prod").id
    repo._service.copy_connection_to_group("web", gid)
    repo.update_connection(
        "web",
        {"nickname": "web2", "hostname": "example.com", "protocol": "ssh"},
        expected_generation=0,
    )
    # The state file must carry the renamed membership so a reload stays valid.
    snap = repo.reload()
    assert [c.id for c in snap.connections] == ["web2"]
    web2 = next(c for c in snap.connections if c.id == "web2")
    assert web2.groups[0].id == gid


def test_create_then_reload_preserves_root_order(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection(
        {"nickname": "new", "hostname": "new.example", "protocol": "ssh"}
    )
    snap = repo.reload()
    assert snap.root_connection_ids == ("web", "new")


def test_delete_grouped_connection_survives_reload(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    gid = repo._service.create_group("Prod").id
    repo._service.copy_connection_to_group("web", gid)
    repo.delete_connection("web")
    snap = repo.reload()  # stale group membership would fail here
    assert snap.connections == ()
    assert snap.groups[0].connection_ids == ()


def test_non_ssh_update_stale_generation_rejected(tmp_path):
    _write_state(
        tmp_path / "connections.json",
        {
            "version": 1,
            "non_ssh_connections": [
                {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
            ],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    repo, root, state = _repo(tmp_path)
    repo.update_connection(
        "tel",
        {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.6"},
        expected_generation=0,
    )
    with pytest.raises(CoreError) as exc:
        repo.update_connection(
            "tel",
            {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.7"},
            expected_generation=0,  # stale: current is 1
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert repo.get_record("tel").hostname == "10.0.0.6"


def test_no_uuid_anywhere(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection(
        {"nickname": "a", "hostname": "a.example", "protocol": "ssh", "uuid": "deadbeef"}
    )
    assert "uuid" not in root.read_text()
    assert "deadbeef" not in root.read_text()
    for record in repo.list_records():
        assert "uuid" not in record.data
