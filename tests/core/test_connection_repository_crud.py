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
from sshpilot.core.connections.ssh_config_store import _atomic_write_text  # noqa: E402
from sshpilot.core.connections.writer_receipt import WriteReceipt  # noqa: E402
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


def _daemon_ssh_write_and_verify(repo, root, text):
    """Perform a real SSH atomic write and verify its receipt.

    Returns the captured-disk state (post-verification) so rollback tests can
    exercise the production authentication flow rather than trusting whatever
    `_record_post_write_locked` happens to find at the path.
    """
    disk_before = repo._capture_transaction_files_locked(Path(root))
    receipt = _atomic_write_text(Path(root), text)
    repo._verify_write_receipt_locked(disk_before, receipt)
    return disk_before


def _daemon_state_write_and_verify(repo, state_path):
    """Perform a real state-file write and verify its receipt."""
    disk_before = repo._capture_transaction_files_locked()
    receipt = repo._persist_state_file_locked()
    repo._verify_write_receipt_locked(disk_before, receipt)
    return disk_before


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


def test_rollback_after_state_persistence_failure_ssh_create(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    pre_bytes = root.read_bytes()
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.create_connection({"nickname": "new", "hostname": "new.example", "protocol": "ssh"})
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    assert root.read_bytes() == pre_bytes


def test_external_edit_during_rollback_raises_ambiguity(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = _daemon_ssh_write_and_verify(
        repo, root, "Host web\n    HostName example.com\n"
    )
    root.write_bytes(b"# externally edited")
    with pytest.raises(CoreError) as exc:
        repo._restore_transaction_files_locked(disk_before)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    # After ambiguity + resync, the repo reflects disk (externally edited).
    repo._resync_from_files()
    snap = repo.snapshot()
    # The externally edited file has no valid hosts, so connections is empty.
    assert snap.connections == ()


def test_no_event_on_rollback(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    changes = []
    repo.add_listener(lambda change: changes.append(change))
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.create_connection({"nickname": "new", "hostname": "new.example", "protocol": "ssh"})
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    assert len(changes) == 0


def test_non_ssh_mutation_does_not_capture_ssh_root(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    pre_ssh = root.read_bytes()
    repo.create_connection({"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"})
    assert root.read_bytes() == pre_ssh


# ---------------------------------------------------------------------------
# Complete rollback test matrix (Commit 2)
# ---------------------------------------------------------------------------


def test_rollback_after_state_failure_ssh_update(tmp_path, monkeypatch):
    """SSH update: if state file write fails, the SSH file must be rolled back."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    # Use 'new' as nickname to avoid collision with 'web' in SSH config.
    repo.create_connection({"nickname": "new", "hostname": "example.com", "protocol": "ssh"})
    pre_bytes = root.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.update_connection("new", {"hostname": "updated.example.com"})
    monkeypatch.undo()
    assert root.read_bytes() == pre_bytes


def test_rollback_after_state_failure_ssh_delete(tmp_path, monkeypatch):
    """SSH delete: if state file write fails, the SSH file must be rolled back."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection({"nickname": "new", "hostname": "example.com", "protocol": "ssh"})
    pre_bytes = root.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.delete_connection("new")
    monkeypatch.undo()
    assert root.read_bytes() == pre_bytes


def test_rollback_after_state_failure_non_ssh_create(tmp_path):
    """Non-SSH create: if state file write fails, the state must be rolled back."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    pre_ssh = root.read_bytes()
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.create_connection({"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"})
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    # SSH root must remain unchanged.
    assert root.read_bytes() == pre_ssh


def test_rollback_after_state_failure_non_ssh_update(tmp_path):
    """Non-SSH update: if state file write fails, the state must be rolled back."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection({"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"})
    snap_before = repo.snapshot()
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.update_connection("tel", {"hostname": "10.0.0.6"})
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    # The connection should still have the old hostname.
    rec = repo.get_record("tel")
    assert rec.data.get("hostname") == "10.0.0.5"


def test_rollback_after_state_failure_non_ssh_delete(tmp_path):
    """Non-SSH delete: if state file write fails, the state must be rolled back."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection({"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"})
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.delete_connection("tel")
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    # The connection should still exist.
    assert repo.get_record("tel") is not None


def test_rollback_after_state_failure_ssh_rename(tmp_path, monkeypatch):
    """SSH rename (update with new nickname): state failure must roll back SSH."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection({"nickname": "new", "hostname": "example.com", "protocol": "ssh"})
    pre_bytes = root.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.update_connection("new", {"nickname": "renamed", "hostname": "example.com"})
    monkeypatch.undo()
    assert root.read_bytes() == pre_bytes


def test_rollback_after_state_failure_ssh_duplicate(tmp_path, monkeypatch):
    """SSH duplicate: state failure must roll back the SSH file."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection({"nickname": "new", "hostname": "example.com", "protocol": "ssh"})
    pre_bytes = root.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.duplicate_connection("new")
    monkeypatch.undo()
    assert root.read_bytes() == pre_bytes


def test_rollback_after_state_failure_ssh_split(tmp_path, monkeypatch):
    """SSH split: state failure must roll back the SSH file."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection({"nickname": "new", "hostname": "example.com", "protocol": "ssh"})
    pre_bytes = root.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )
    with pytest.raises((CoreError, OSError)):
        repo.split_connection("new", "new", {"hostname": "split.example.com", "username": "u", "protocol": "ssh"})
    monkeypatch.undo()
    assert root.read_bytes() == pre_bytes


def test_external_edit_after_daemon_state_write_raises_ambiguity(tmp_path, monkeypatch):
    """After daemon writes the state file, an external edit must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    # Perform a real state-file write via the production writer and verify
    # the receipt, then simulate an external edit before rollback.
    disk_before = _daemon_state_write_and_verify(repo, state)
    # Now simulate an external edit.
    state.write_text("{\"externally\": \"edited\"}")
    # Restore should detect the external edit.
    with pytest.raises(CoreError) as exc:
        repo._restore_transaction_files_locked(disk_before)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_symlink_swap_during_post_write_raises_ambiguity(tmp_path):
    """Symlink swap during receipt verification must raise MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)
    # Daemon writes the file (producing a receipt)...
    receipt = _atomic_write_text(root, "Host web\n    HostName example.com\n")
    # ...then a symlink is swapped in between writer completion and
    # the repository's receipt verification.
    root.unlink()
    root.symlink_to("/etc/hostname")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_rollback_write_failure_raises_ambiguity(tmp_path, monkeypatch):
    """If rollback write itself fails, the repository reports ambiguity."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = _daemon_ssh_write_and_verify(
        repo, root, "Host web\n    HostName example.com\n"
    )
    # Patch _atomic_write_text to simulate a write failure during restore.
    def fail_atomic(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository._atomic_write_text",
        fail_atomic,
    )
    with pytest.raises(CoreError) as exc:
        repo._restore_transaction_files_locked(disk_before)
    assert exc.value.code in (ErrorCode.MUTATION_AMBIGUOUS, ErrorCode.CONNECTION_STATE_IO_ERROR)


def test_exact_mode_and_existence_restoration(tmp_path):
    """Rollback restores exact file mode and existence state."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    import os
    os.chmod(str(root), 0o600)
    disk_before = repo._capture_transaction_files_locked(root)
    # Verify mode was captured.
    assert disk_before[root][2] == 0o600
    # Daemon write via the real atomic writer (preserves the existing 0o600 mode).
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    repo._verify_write_receipt_locked(disk_before, receipt)
    # Rollback should restore the original mode.
    repo._restore_transaction_files_locked(disk_before)
    st = os.stat(str(root))
    assert (st.st_mode & 0o777) == 0o600


def test_unchanged_generation_and_memory(tmp_path):
    """Rollback does not change the generation counter."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    gen_before = repo._generation
    disk_before = repo._capture_transaction_files_locked()
    # Simulate a failed mutation that rolls back.
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.create_connection({"nickname": "new", "hostname": "new.example", "protocol": "ssh"})
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    assert repo._generation == gen_before


# ---------------------------------------------------------------------------
# Commit 2: Writer-authenticated rollback token tests
# ---------------------------------------------------------------------------


def test_non_ssh_create_captures_no_ssh_path(tmp_path):
    """Failed non-SSH mutation must not capture or restore the SSH root."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    pre_ssh = root.read_bytes()
    # Capture what a non-SSH mutation would capture.
    disk_before = repo._capture_transaction_files_locked()  # no ssh_target
    # The SSH root must not be in the captured set.
    assert root not in disk_before
    assert state in disk_before
    # The SSH bytes must remain unchanged after any operation.
    repo.create_connection({"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"})
    assert root.read_bytes() == pre_ssh


def test_non_ssh_create_production_path_captures_no_ssh_root(tmp_path, monkeypatch):
    """Production-path test intercepting the SSH capture for non-SSH create.

    We monkeypatch ``_capture_transaction_files_locked`` to record the
    ``ssh_target`` argument it was called with from the production
    ``create_connection`` flow, then force the state-file write to fail so
    the mutation rolls back.  The intercepted capture must prove the SSH
    root was *never* captured for a non-SSH creation.
    """
    import os as _os

    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    pre_ssh = root.read_bytes()

    capture_calls = []
    original_capture = repo._capture_transaction_files_locked

    def capturing(ssh_target=None):
        capture_calls.append(ssh_target)
        return original_capture(ssh_target)

    repo._capture_transaction_files_locked = capturing

    def fail_write(*_args, **_kwargs):
        raise OSError("injected state write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_connection_state",
        fail_write,
    )

    with pytest.raises(OSError):
        repo.create_connection(
            {"nickname": "telnet-1", "protocol": "telnet", "hostname": "10.0.0.5"}
        )
    # The production path must capture only the state file (ssh_target=None);
    # the SSH root must never be captured for a non-SSH creation.
    assert all(call is None for call in capture_calls)
    assert root.read_bytes() == pre_ssh


def _hook_replace_into_regular(path: Path, content: bytes) -> Path:
    """Replace *path* with a fresh regular file holding *content* (new inode)."""
    path.unlink()
    path.write_bytes(content)
    return path


def _hook_replace_into_symlink(path: Path, target: str) -> Path:
    path.unlink()
    path.symlink_to(target)
    return path


def _hook_replace_into_directory(path: Path) -> Path:
    path.unlink()
    path.mkdir()
    return path


def _install_race_hook(repo, hook):
    """Install a one-shot ``_pre_verify_hook`` on a repository."""
    def _trigger(receipt):
        # Reset before invoking so the hook itself can resubmit if desired.
        repo._pre_verify_hook = None
        hook(receipt)
    repo._pre_verify_hook = _trigger


def test_regular_file_replaced_with_identical_bytes_different_inode(tmp_path):
    """Replacement with identical bytes but a different inode must cause MUTATION_AMBIGUOUS
    at receipt-verification time, before the file is marked daemon-owned."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    original_bytes = b"Host new\n    HostName new.example\n"

    def hook(receipt):
        _hook_replace_into_regular(receipt.path, receipt.expected_bytes)

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, original_bytes.decode("utf-8"))
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_regular_file_replaced_with_different_bytes(tmp_path):
    """Replacement with different bytes at the hook must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        _hook_replace_into_regular(
            receipt.path, b"Host different\n    HostName other.com\n",
        )

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_regular_file_replaced_with_same_bytes_different_mode(tmp_path):
    """A same-bytes, different-mode replacement must cause MUTATION_AMBIGUOUS."""
    import os as _os
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        _hook_replace_into_regular(receipt.path, receipt.expected_bytes)
        _os.chmod(str(receipt.path), 0o644)

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_regular_file_replaced_with_symlink_at_hook(tmp_path):
    """A symlink replacement before receipt verification must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        _hook_replace_into_symlink(receipt.path, "/etc/hostname")

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_regular_file_replaced_with_directory_at_hook(tmp_path):
    """A directory replacement before receipt verification must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        _hook_replace_into_directory(receipt.path)

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_newly_created_file_replaced_at_hook(tmp_path):
    """A newly created file replaced at the hook must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        _hook_replace_into_regular(
            receipt.path, b"Host replaced\n    HostName replaced.com\n",
        )

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host web\n    HostName example.com\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_state_file_replaced_at_hook(tmp_path):
    """State file replacement at the hook must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked()

    def hook(receipt):
        _hook_replace_into_regular(
            receipt.path, b"{\"externally\": \"replaced\"}",
        )

    _install_race_hook(repo, hook)
    receipt = repo._persist_state_file_locked()
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_post_write_digest_mismatch(tmp_path):
    """A digest (bytes-) mismatch at receipt verification must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        # Overwrite same file in-place so dev/inode/mode are unchanged but
        # the bytes differ — exercises the digest check specifically.
        receipt.path.write_bytes(b"# rewritten bytes")

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_post_write_identity_mismatch(tmp_path):
    """A device/inode replacement at the hook must cause MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        # Replace with a new file having the same bytes (digest matches) but
        # a different inode — exercises the identity check specifically.
        _hook_replace_into_regular(receipt.path, receipt.expected_bytes)

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_post_write_mode_mismatch(tmp_path):
    """A mode-only external edit at the hook must cause MUTATION_AMBIGUOUS."""
    import os as _os
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)

    def hook(receipt):
        # Same file (same dev/inode/bytes), only the mode changed.
        _os.chmod(str(receipt.path), 0o644)

    _install_race_hook(repo, hook)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, receipt)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_missing_receipt_rejected(tmp_path):
    """A missing receipt (None) must never be trusted — MUTATION_AMBIGUOUS."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)
    with pytest.raises(CoreError) as exc:
        repo._verify_write_receipt_locked(disk_before, None)
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_chmod_failure_during_rollback_raises_ambiguity(tmp_path, monkeypatch):
    """chmod() failure during rollback must raise MUTATION_AMBIGUOUS.

    Deterministic — monkeypatches ``os.chmod`` so the failure occurs only
    during the rollback mode restoration.  No chattr, no filesystem support,
    no privileges, no skips.
    """
    import os as _os
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = _daemon_ssh_write_and_verify(
        repo, root, "Host web\n    HostName example.com\n",
    )
    orig_chmod = _os.chmod
    rollback_in_progress = []

    def fail_chmod(path, mode, *args, **kwargs):
        # The repository's mode-restoration chmod is the only chmod call
        # that passes ``follow_symlinks=False``; the atomic writer's own
        # chmod uses a positional mode.  Failing only the rollback chmod
        # isolates the ambiguity deterministically.
        if rollback_in_progress and kwargs.get("follow_symlinks") is False:
            raise OSError("chmod failed during rollback")
        return orig_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(_os, "chmod", fail_chmod)

    def _restore_and_mark():
        rollback_in_progress.append(True)
        try:
            repo._restore_transaction_files_locked(disk_before)
        finally:
            rollback_in_progress.pop()

    with pytest.raises(CoreError) as exc:
        _restore_and_mark()
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS


def test_successful_receipt_verification_ssh_write(tmp_path):
    """A successful SSH write must pass receipt verification and mark daemon-owned."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked(root)
    receipt = _atomic_write_text(root, "Host new\n    HostName new.example\n")
    # Must not raise.
    repo._verify_write_receipt_locked(disk_before, receipt)
    # The receipt is recorded for rollback verification.
    assert disk_before[root][3] is receipt
    # Rollback restores the original content (using the production path).
    repo._restore_transaction_files_locked(disk_before)
    assert root.read_text() == "Host web\n    HostName example.com\n"


def test_successful_receipt_verification_state_write(tmp_path):
    """A successful state-file write must pass receipt verification."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    disk_before = repo._capture_transaction_files_locked()
    receipt = repo._persist_state_file_locked()
    # Must not raise.
    repo._verify_write_receipt_locked(disk_before, receipt)
    assert disk_before[state][3] is receipt


def test_no_event_and_unchanged_generation_after_successful_rollback(tmp_path):
    """After a successful rollback, no event is published and generation is unchanged."""
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    gen_before = repo._generation
    changes = []
    repo.add_listener(lambda change: changes.append(change))
    # Force a rollback scenario.
    state.unlink()
    state.mkdir()
    try:
        with pytest.raises(CoreError):
            repo.create_connection({"nickname": "new", "hostname": "new.example", "protocol": "ssh"})
    finally:
        state.rmdir()
        _write_state(state, {"version": 1, "non_ssh_connections": [], "groups": {"groups": {}, "root_connections": []}, "metadata": {}})
    assert len(changes) == 0
    assert repo._generation == gen_before
