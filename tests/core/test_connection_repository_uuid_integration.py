"""Production repository coverage for internal UUID identity with alias API IDs."""

from __future__ import annotations

from pathlib import Path
import os

import pytest

from sshpilot.api.models.connections import SaveSshConfigTextRequest
from sshpilot.core.connections.identity_state_v2 import IdentityStateV2
from sshpilot.core.connections.identity_state_v2 import ConnectionReference
from sshpilot.core.connections.identity_state_v2 import ReferenceKind
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections import repository as repository_module
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.core.connections.state_file import (
    identity_transaction_intent_path,
    read_pending_identity_transaction,
    read_identity_state_v2,
)


def _repo(tmp_path: Path, text: str) -> tuple[ConnectionRepository, Path, Path]:
    root = tmp_path / "ssh_config"
    root.write_text(text, encoding="utf-8")
    state = tmp_path / "connections.json"
    return (
        ConnectionRepository(
            ssh_store=SshConfigStore(root, isolated=True),
            state_path=state,
            legacy_config_path=tmp_path / "config.json",
            isolated=True,
        ),
        root,
        state,
    )


def test_startup_migrates_to_v2_and_keeps_uuid_across_restart(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName server.example\n    User deploy\n"
    )
    first = read_identity_state_v2(state)
    assert isinstance(first, IdentityStateV2)
    uuid = first.identities[0].uuid
    assert repo.snapshot().connections[0].id == "old"
    assert repo.snapshot().connections[0].display_name == "old"
    del repo
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    second = read_identity_state_v2(state)
    assert second.identities[0].uuid == uuid
    assert restarted.snapshot().connections[0].id == "old"


def test_external_safe_rename_uses_persisted_projection(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName server.example\n    User deploy\n"
    )
    uuid = read_identity_state_v2(state).identities[0].uuid
    root.write_text("Host new\n    HostName server.example\n    User deploy\n")
    assert repo.reload().connections[0].id == "new"
    persisted = read_identity_state_v2(state)
    assert persisted.identities[0].uuid == uuid
    assert persisted.identities[0].projection.alias == "new"


def test_external_two_way_collision_remains_unresolved(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "Host old-a\n    HostName server.example\n    User deploy\n\n"
        "Host old-b\n    HostName server.example\n    User deploy\n",
    )
    old_uuids = {item.uuid for item in read_identity_state_v2(state).identities}
    root.write_text(
        "Host new-b\n    HostName server.example\n    User deploy\n\n"
        "Host new-a\n    HostName server.example\n    User deploy\n"
    )
    snapshot = repo.reload()
    persisted = read_identity_state_v2(state)
    assert {item.id for item in snapshot.connections} == {"new-a", "new-b"}
    assert persisted.pending_ambiguities
    assert {item.uuid for item in persisted.identities} == old_uuids
    assert persisted.last_reconciled_ssh_revision != persisted.observed_ssh_revision
    assert snapshot.metadata == ()


def test_managed_alias_and_destination_change_keeps_uuid(tmp_path):
    repo, _root, state = _repo(
        tmp_path, "Host old\n    HostName old.example\n    User deploy\n"
    )
    uuid = read_identity_state_v2(state).identities[0].uuid
    repo.update_connection(
        "old",
        {
            "nickname": "new",
            "hostname": "new.example",
            "username": "deploy",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    persisted = read_identity_state_v2(state)
    assert persisted.identities[0].uuid == uuid
    assert persisted.identities[0].projection.alias == "new"
    assert repo.snapshot().connections[0].id == "new"


@pytest.mark.parametrize("pattern_before", [True, False])
def test_managed_rename_preserves_pattern_rule_and_uuid(tmp_path, pattern_before):
    pattern = "Host new *\n    User deploy\n    # preserve\n"
    concrete = "Host old\n    HostName old.example\n"
    text = (pattern + "\n" + concrete) if pattern_before else (concrete + "\n" + pattern)
    repo, root, state = _repo(tmp_path, text)
    uuid = read_identity_state_v2(state).identities[0].uuid
    repo.set_display_name("old", "Production Old")
    repo.update_connection(
        "old",
        {"nickname": "new", "hostname": "old.example", "protocol": "ssh"},
        expected_generation=0,
    )
    updated = root.read_text()
    assert pattern in updated
    assert "Host new\n    HostName old.example\n" in updated
    persisted = read_identity_state_v2(state)
    assert persisted.identities[0].uuid == uuid
    assert persisted.identities[0].display_name == "Production Old"
    assert persisted.identities[0].projection.alias == "new"


def test_managed_rename_rejects_empty_concrete_destination_without_side_effects(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "Host new\n    # reserved\n\n"
        "Host old\n    HostName old.example\n",
    )
    before_root = root.read_bytes()
    before_state = state.read_bytes()
    events = []
    repo.add_listener(events.append)
    with pytest.raises(CoreError) as exc:
        repo.update_connection(
            "old",
            {"nickname": "new", "hostname": "old.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before_root
    assert state.read_bytes() == before_state
    assert not identity_transaction_intent_path(state).exists()
    assert events == []


def test_repository_create_rejects_empty_authored_alias_without_side_effects(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "Host reserved\n    # authored reservation\n",
    )
    before_root = root.read_bytes()
    before_state = state.read_bytes()
    before_snapshot = repo.snapshot()
    events = []
    repo.add_listener(events.append)
    with pytest.raises(CoreError) as exc:
        repo.create_connection(
            {"nickname": "reserved", "hostname": "reserved.example", "protocol": "ssh"}
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before_root
    assert state.read_bytes() == before_state
    assert repo.snapshot() == before_snapshot
    assert not identity_transaction_intent_path(state).exists()
    assert events == []


def test_repository_split_rejects_empty_authored_alias_without_side_effects(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "Host reserved\n    # authored reservation\n\n"
        "Host old\n    HostName old.example\n",
    )
    before_root = root.read_bytes()
    before_state = state.read_bytes()
    before_snapshot = repo.snapshot()
    events = []
    repo.add_listener(events.append)
    with pytest.raises(CoreError) as exc:
        repo.split_connection(
            "old",
            "old",
            {"nickname": "reserved", "hostname": "reserved.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert root.read_bytes() == before_root
    assert state.read_bytes() == before_state
    assert repo.snapshot() == before_snapshot
    assert not identity_transaction_intent_path(state).exists()
    assert events == []


def test_repository_duplicate_skips_empty_authored_copy_alias(tmp_path):
    repo, root, _state = _repo(
        tmp_path,
        "Host old-Copy\n    # authored reservation\n\n"
        "Host old\n    HostName old.example\n",
    )
    duplicate = repo.duplicate_connection("old")
    assert duplicate.id == "old-Copy-2"
    assert "Host old-Copy\n    # authored reservation\n" in root.read_text()
    assert root.read_text().count("Host old-Copy-2\n") == 1


def test_repository_same_name_split_advances_generation_once_and_preserves_uuid(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "Host old\n    HostName old.example\n",
    )
    uuid = read_identity_state_v2(state).identities[0].uuid
    events = []
    repo.add_listener(events.append)

    result = repo.split_connection(
        "old",
        "old",
        {"nickname": "old", "hostname": "new.example", "protocol": "ssh"},
        expected_generation=0,
    )
    assert result.id == "old"
    assert result.generation == 1
    assert repo.get_record("old").generation == 1
    assert repo.get_editor_record("old").generation == 1
    assert read_identity_state_v2(state).identities[0].uuid == uuid
    assert not identity_transaction_intent_path(state).exists()
    assert len(events) == 1

    before = root.read_bytes()
    with pytest.raises(CoreError) as exc:
        repo.update_connection(
            "old",
            {"nickname": "old", "hostname": "stale.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert root.read_bytes() == before
    assert len(events) == 1

    updated = repo.update_connection(
        "old",
        {"nickname": "old", "hostname": "current.example", "protocol": "ssh"},
        expected_generation=1,
    )
    assert updated.generation == 2
    assert repo.get_record("old").generation == 2
    assert repo.get_editor_record("old").generation == 2
    assert not identity_transaction_intent_path(state).exists()
    assert len(events) == 2


def test_managed_noop_ssh_update_with_display_name_builds_valid_intent(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n    User deploy\n"
    )
    before = root.read_bytes()
    uuid = read_identity_state_v2(state).identities[0].uuid

    repo.update_connection(
        "prod",
        {
            "nickname": "prod",
            "hostname": "server.example",
            "username": "deploy",
            "protocol": "ssh",
            "display_name": "Production Server",
        },
        expected_generation=0,
    )

    persisted = read_identity_state_v2(state)
    assert persisted.identities[0].uuid == uuid
    assert persisted.identities[0].display_name == "Production Server"
    assert root.read_bytes() == before


def test_raw_editor_uses_uuid_reconciliation(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName server.example\n    User deploy\n"
    )
    uuid = read_identity_state_v2(state).identities[0].uuid
    revision = repo.get_ssh_config_text().revision
    repo.save_ssh_config_text(
        SaveSshConfigTextRequest(
            text="Host new\n    HostName server.example\n    User deploy\n",
            expected_revision=revision,
        )
    )
    assert read_identity_state_v2(state).identities[0].uuid == uuid
    assert root.read_text().startswith("Host new")


def test_display_name_is_sidecar_only_and_public_id_is_alias(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n    User deploy\n"
    )
    before = root.read_bytes()
    identity = read_identity_state_v2(state).identities[0]
    repo.set_display_name("prod", "Production Server / تهران")
    summary = repo.snapshot().connections[0]
    assert summary.id == "prod"
    assert summary.display_name == "Production Server / تهران"
    assert read_identity_state_v2(state).identities[0].uuid == identity.uuid
    assert root.read_bytes() == before


def test_display_name_write_failure_does_not_leave_memory_ahead_of_sidecar(
    tmp_path, monkeypatch
):
    repo, _root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )
    original = read_identity_state_v2(state).identities[0].display_name

    def fail_write(*_args, **_kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(
        "sshpilot.core.connections.repository.write_identity_state_v2",
        fail_write,
    )
    with pytest.raises(OSError):
        repo.set_display_name("prod", "Production Server")

    assert repo.snapshot().connections[0].display_name == original
    assert read_identity_state_v2(state).identities[0].display_name == original


def test_crash_after_intent_before_ssh_recovers_base(tmp_path):
    repo, root, state = _repo(tmp_path, "Host prod\n    HostName server.example\n")

    class Crash(BaseException):
        pass

    original = repo._ssh_store.commit_prepared

    def crash(_prepared):
        assert identity_transaction_intent_path(state).exists()
        raise Crash()

    repo._ssh_store.commit_prepared = crash
    try:
        with pytest.raises(Crash):
            repo.update_connection(
                "prod",
                {"nickname": "prod2", "hostname": "new.example", "protocol": "ssh"},
                expected_generation=0,
            )
    finally:
        repo._ssh_store.commit_prepared = original
    assert root.read_text(encoding="utf-8").startswith("Host prod")
    assert identity_transaction_intent_path(state).exists()

    restarted, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))
    assert restarted.snapshot().connections[0].id == "prod"
    assert not identity_transaction_intent_path(state).exists()


def test_crash_after_ssh_before_sidecar_recovers_target(tmp_path, monkeypatch):
    repo, root, state = _repo(tmp_path, "Host prod\n    HostName server.example\n")

    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    monkeypatch.setattr(repo, "_persist_state_file_locked", crash)
    with pytest.raises(Crash):
        repo.update_connection(
            "prod",
            {"nickname": "prod2", "hostname": "new.example", "protocol": "ssh"},
            expected_generation=0,
        )
    old_uuid = read_identity_state_v2(state).identities[0].uuid
    assert root.read_text(encoding="utf-8").startswith("Host prod2")
    assert identity_transaction_intent_path(state).exists()

    restarted, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))
    recovered = read_identity_state_v2(state)
    assert restarted.snapshot().connections[0].id == "prod2"
    assert recovered.identities[0].uuid == old_uuid
    assert not identity_transaction_intent_path(state).exists()


def _inject_intent_clear_failure(monkeypatch, *, unlink_first: bool):
    real_clear = repository_module.clear_pending_identity_transaction
    calls = {"count": 0}

    def fail_once(path):
        calls["count"] += 1
        if calls["count"] == 1:
            if unlink_first:
                os.unlink(path)
            raise OSError("injected intent cleanup failure")
        return real_clear(path)

    monkeypatch.setattr(
        repository_module,
        "clear_pending_identity_transaction",
        fail_once,
    )
    return calls


@pytest.mark.parametrize("unlink_first", [False, True])
def test_update_intent_cleanup_failure_restores_both_resources(
    tmp_path, monkeypatch, unlink_first
):
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName old.example\n    User deploy\n"
    )
    base_root = root.read_bytes()
    base_sidecar = state.read_bytes()
    base_generation = read_identity_state_v2(state).sidecar_generation
    _inject_intent_clear_failure(monkeypatch, unlink_first=unlink_first)

    with pytest.raises(OSError, match="injected intent cleanup failure"):
        repo.update_connection(
            "old",
            {"nickname": "new", "hostname": "new.example", "protocol": "ssh"},
            expected_generation=0,
        )

    assert root.read_bytes() == base_root
    assert state.read_bytes() == base_sidecar
    assert read_identity_state_v2(state).sidecar_generation == base_generation
    assert repo.snapshot().connections[0].id == "old"
    assert not identity_transaction_intent_path(state).exists()


def test_create_intent_cleanup_failure_leaves_no_target_identity(
    tmp_path, monkeypatch
):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName prod.example\n"
    )
    base_root = root.read_bytes()
    base_sidecar = state.read_bytes()
    _inject_intent_clear_failure(monkeypatch, unlink_first=True)

    with pytest.raises(OSError, match="injected intent cleanup failure"):
        repo.create_connection(
            {"nickname": "copy", "hostname": "copy.example", "protocol": "ssh"}
        )

    restored = read_identity_state_v2(state)
    assert root.read_bytes() == base_root
    assert state.read_bytes() == base_sidecar
    assert {item.projection.alias for item in restored.identities} == {"prod"}
    assert not identity_transaction_intent_path(state).exists()


def test_duplicate_intent_cleanup_failure_restores_group_and_uuid_state(
    tmp_path, monkeypatch
):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName prod.example\n"
    )
    group = repo.create_group("Production")
    repo.copy_connection_to_group("prod", group.id)
    base_root = root.read_bytes()
    base_sidecar = state.read_bytes()
    base_state = read_identity_state_v2(state)
    _inject_intent_clear_failure(monkeypatch, unlink_first=False)

    with pytest.raises(OSError, match="injected intent cleanup failure"):
        repo.duplicate_connection("prod")

    restored = read_identity_state_v2(state)
    assert root.read_bytes() == base_root
    assert state.read_bytes() == base_sidecar
    assert restored == base_state
    assert repo.get_record("prod-Copy") is None
    assert not identity_transaction_intent_path(state).exists()


def test_delete_intent_cleanup_failure_restores_tombstone_and_ssh_state(
    tmp_path, monkeypatch
):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName prod.example\n"
    )
    base_root = root.read_bytes()
    base_sidecar = state.read_bytes()
    _inject_intent_clear_failure(monkeypatch, unlink_first=True)

    with pytest.raises(OSError, match="injected intent cleanup failure"):
        repo.delete_connection("prod")

    assert root.read_bytes() == base_root
    assert state.read_bytes() == base_sidecar
    assert repo.get_record("prod") is not None
    assert not identity_transaction_intent_path(state).exists()


def test_split_intent_cleanup_failure_restores_original_config_and_state(
    tmp_path, monkeypatch
):
    repo, root, state = _repo(
        tmp_path, "Host db jump\n    HostName=db.internal\n    User dbuser\n"
    )
    base_root = root.read_bytes()
    base_sidecar = state.read_bytes()
    _inject_intent_clear_failure(monkeypatch, unlink_first=True)

    with pytest.raises(OSError, match="injected intent cleanup failure"):
        repo.split_connection(
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

    assert root.read_bytes() == base_root
    assert state.read_bytes() == base_sidecar
    assert repo.get_record("jump") is not None
    assert repo.get_record("jump2") is None
    assert not identity_transaction_intent_path(state).exists()


def test_sidecar_write_durability_failure_recovers_without_blocking(
    tmp_path, monkeypatch
):
    """An uncertain sidecar replace must not be mistaken for a clean commit.

    The note left by such a write can never be placed again -- the sidecar
    has moved past its base while the SSH root is back at it -- so it is
    discarded and the sidecar is reconciled against the SSH configuration.
    The uncommitted target must not survive that, and the repository must
    stay writable rather than refusing every later save.
    """
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName old.example\n"
    )
    base_root = root.read_bytes()
    real_write = repository_module.write_identity_state_v2

    def write_then_fail(path, target):
        real_write(path, target)
        raise OSError("injected sidecar durability failure")

    monkeypatch.setattr(
        repository_module, "write_identity_state_v2", write_then_fail
    )
    with pytest.raises(OSError, match="injected sidecar durability failure"):
        repo.update_connection(
            "old",
            {"nickname": "new", "hostname": "new.example", "protocol": "ssh"},
            expected_generation=0,
        )

    assert root.read_bytes() == base_root
    assert not identity_transaction_intent_path(state).exists()
    del repo

    # The durability glitch passes; the next start must come up usable.
    monkeypatch.setattr(
        repository_module, "write_identity_state_v2", real_write
    )
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    assert restarted._identity_state_unavailable is False
    assert [item.id for item in restarted.snapshot().connections] == ["old"]
    active = [
        item for item in read_identity_state_v2(state).identities
        if not item.tombstone
    ]
    assert [item.projection.alias for item in active] == ["old"]
    assert restarted.create_connection(
        {"nickname": "fresh", "hostname": "fresh.example", "protocol": "ssh"}
    ).id == "fresh"


def test_managed_create_allocates_new_uuid_for_duplicate_destination(tmp_path):
    repo, _root, state = _repo(tmp_path, "Host prod\n    HostName server.example\n")
    original = read_identity_state_v2(state).identities[0].uuid
    repo.create_connection(
        {"nickname": "copy", "hostname": "server.example", "protocol": "ssh"}
    )
    identities = read_identity_state_v2(state).identities
    assert {item.projection.alias for item in identities} == {"prod", "copy"}
    assert len({item.uuid for item in identities}) == 2
    assert original in {item.uuid for item in identities}


def test_managed_create_persists_the_uuid_from_the_pending_target(tmp_path):
    repo, _root, state = _repo(tmp_path, "Host prod\n    HostName server.example\n")
    planned = {}
    original_commit = repo._ssh_store.commit_prepared

    def capture(prepared):
        intent = read_pending_identity_transaction(
            identity_transaction_intent_path(state)
        )
        planned[prepared.connection_id] = next(
            item.uuid for item in intent.target_state.identities
            if item.projection.alias == prepared.connection_id
        )
        return original_commit(prepared)

    repo._ssh_store.commit_prepared = capture
    try:
        repo.create_connection(
            {"nickname": "copy", "hostname": "server.example", "protocol": "ssh"}
        )
    finally:
        repo._ssh_store.commit_prepared = original_commit
    state_after = read_identity_state_v2(state)
    assert next(
        item.uuid for item in state_after.identities
        if item.projection.alias == "copy"
    ) == planned["copy"]


def test_duplicate_target_contains_copied_group_before_intent(tmp_path):
    repo, _root, state = _repo(tmp_path, "Host prod\n    HostName server.example\n")
    group = repo.create_group("Production")
    repo.copy_connection_to_group("prod", group.id)
    captured = {}
    original_commit = repo._ssh_store.commit_prepared

    def capture(prepared):
        intent = read_pending_identity_transaction(
            identity_transaction_intent_path(state)
        )
        captured["target"] = intent.target_state
        return original_commit(prepared)

    repo._ssh_store.commit_prepared = capture
    try:
        repo.duplicate_connection("prod")
    finally:
        repo._ssh_store.commit_prepared = original_commit
    target = captured["target"]
    duplicate = next(
        item for item in target.identities
        if item.projection.alias == "prod-Copy"
    )
    assert ConnectionReference(ReferenceKind.SSH_UUID, duplicate.uuid) in next(
        item for item in target.groups if item.id == group.id
    ).members


def test_display_name_noop_does_not_rewrite_sidecar(tmp_path):
    repo, _root, state = _repo(tmp_path, "Host prod\n    HostName server.example\n")
    before = state.stat()
    generation = read_identity_state_v2(state).sidecar_generation
    repo.set_display_name("prod", "prod")
    after = state.stat()
    assert read_identity_state_v2(state).sidecar_generation == generation
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_corrupt_pending_intent_is_discarded_and_keeps_identity(tmp_path):
    """A note that cannot be parsed says nothing about the sidecar.

    It is thrown away rather than treated as a reason to distrust identity
    state: the sidecar still reads cleanly, so the app keeps the display name
    it holds and stays writable.
    """
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )
    repo.set_display_name("prod", "Production")
    pending = identity_transaction_intent_path(state)
    pending.write_text("{broken", encoding="utf-8")
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    assert restarted._identity_state_unavailable is False
    assert restarted.snapshot().connections[0].display_name == "Production"
    assert not pending.exists()
    restarted.set_display_name("prod", "Renamed")
    assert restarted.snapshot().connections[0].display_name == "Renamed"


def test_ambiguous_alias_rejects_identity_mutations_but_remains_visible(tmp_path):
    repo, root, state = _repo(
        tmp_path,
        "Host old-a\n    HostName server.example\n    User deploy\n\n"
        "Host old-b\n    HostName server.example\n    User deploy\n",
    )
    root.write_text(
        "Host new-a\n    HostName server.example\n    User deploy\n\n"
        "Host new-b\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    repo.reload()
    assert {item.id for item in repo.snapshot().connections} == {"new-a", "new-b"}
    with pytest.raises(CoreError) as exc:
        repo.set_display_name("new-a", "No ownership")
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    with pytest.raises(CoreError) as exc:
        repo.update_connection_metadata("new-b", {"pinned": True})
    assert exc.value.code is ErrorCode.MUTATION_AMBIGUOUS
    persisted = read_identity_state_v2(state)
    assert persisted.pending_ambiguities


def test_dangling_pending_intent_symlink_is_ignored_not_followed(tmp_path):
    """A planted symlink is never read or written through, and never blocks.

    ``clear_pending_identity_transaction`` still refuses to operate through
    the link, so it stays on disk; ignoring it is safe precisely because it
    is never followed, and the repository must remain writable regardless.
    """
    repo, _root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )
    intent_path = identity_transaction_intent_path(state)
    intent_path.symlink_to(tmp_path / "missing-intent")

    restarted, _root, _state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )

    assert restarted.snapshot().connections[0].id == "prod"
    assert restarted._identity_state_unavailable is False
    assert intent_path.is_symlink()
    assert not (tmp_path / "missing-intent").exists()
    restarted.set_display_name("prod", "Available")
    assert restarted.snapshot().connections[0].display_name == "Available"
    assert not (tmp_path / "missing-intent").exists()


def test_groups_and_metadata_follow_uuid_across_external_alias_rename(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName server.example\n    User deploy\n"
    )
    group = repo.create_group("Production")
    repo.copy_connection_to_group("old", group.id)
    repo.update_connection_metadata("old", {"pinned": True, "tags": ["prod"]})
    old_state = read_identity_state_v2(state)
    uuid = old_state.identities[0].uuid
    assert old_state.groups[0].members == (
        ConnectionReference(ReferenceKind.SSH_UUID, uuid),
    )
    assert old_state.metadata[uuid]["pinned"] is True

    root.write_text(
        "Host new\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    snapshot = repo.reload()
    assert snapshot.connections[0].id == "new"
    assert snapshot.connections[0].groups[0].id == group.id
    assert snapshot.metadata[0].connection_id == "new"
    new_state = read_identity_state_v2(state)
    assert new_state.identities[0].uuid == uuid
    assert new_state.groups[0].members[0].kind is ReferenceKind.SSH_UUID
    assert new_state.groups[0].members[0].value == uuid
    assert new_state.metadata[uuid]["tags"] == ["prod"]


def test_delete_then_alias_reuse_allocates_new_uuid_and_keeps_tombstone(tmp_path):
    repo, _root, state = _repo(
        tmp_path, "Host old\n    HostName server.example\n"
    )
    old_uuid = read_identity_state_v2(state).identities[0].uuid
    repo.delete_connection("old")
    repo.create_connection(
        {"nickname": "old", "hostname": "other.example", "protocol": "ssh"}
    )
    persisted = read_identity_state_v2(state)
    active = [item for item in persisted.identities if not item.tombstone]
    tombstones = [item for item in persisted.identities if item.tombstone]
    assert len(active) == 1
    assert active[0].projection.alias == "old"
    assert active[0].uuid != old_uuid
    assert any(item.uuid == old_uuid for item in tombstones)


def test_clean_rollback_leaves_no_orphaned_pending_intent(tmp_path, monkeypatch):
    """Rollback's resync must classify its own note and clear it.

    Nothing else would: the only other caller of
    ``clear_pending_identity_transaction`` runs at the end of a *successful*
    mutation, and a surviving note that later fails to classify degrades
    identity state permanently.  Rollback restores the sidecar and SSH root
    to base, so the note recovers as ABORT_BASE during ``_resync_from_files``.
    """
    repo, root, state = _repo(tmp_path, "Host old\n    HostName old.example\n")
    base_root = root.read_bytes()
    base_sidecar = state.read_bytes()

    def fail_commit(_prepared):
        raise OSError("injected SSH commit failure")

    monkeypatch.setattr(repo._ssh_store, "commit_prepared", fail_commit)
    with pytest.raises(OSError, match="injected SSH commit failure"):
        repo.update_connection(
            "old",
            {"nickname": "new", "hostname": "new.example", "protocol": "ssh"},
            expected_generation=0,
        )

    assert root.read_bytes() == base_root
    assert state.read_bytes() == base_sidecar
    assert not identity_transaction_intent_path(state).exists()
    assert repo._identity_state_unavailable is False


def _leave_orphaned_intent(monkeypatch, repo, **connection):
    """Commit a mutation but skip the note cleanup, as a crash would."""
    monkeypatch.setattr(
        repository_module, "clear_pending_identity_transaction", lambda _p: None
    )
    repo.create_connection(connection)
    monkeypatch.undo()


def test_unplaceable_pending_intent_is_discarded_instead_of_blocking(
    tmp_path, monkeypatch
):
    """The field failure: a note that survived a crash bricked every save.

    Recovery classified it as STALE_INTENT, which dropped identity state and
    disabled identity-owned mutations -- and nothing could lift that, because
    the note is only cleared at the end of a *successful* mutation.  Every
    restart re-degraded, so saving stayed broken on that install forever.
    """
    repo, root, state = _repo(tmp_path, "Host old\n    HostName old.example\n")
    _leave_orphaned_intent(
        monkeypatch, repo, nickname="a", hostname="a.example", protocol="ssh"
    )
    assert identity_transaction_intent_path(state).exists()
    del repo

    # An external edit, so the note now matches neither base nor target.
    root.write_text(
        root.read_text() + "\nHost manual\n    HostName manual.example\n",
        encoding="utf-8",
    )
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    assert restarted._identity_state_unavailable is False
    assert not identity_transaction_intent_path(state).exists()
    assert restarted.create_connection(
        {"nickname": "fresh", "hostname": "fresh.example", "protocol": "ssh"}
    ).id == "fresh"


def test_discarding_a_pending_intent_keeps_names_groups_and_metadata(
    tmp_path, monkeypatch
):
    """Discarding the note must not cost the user their organization.

    The sidecar is still readable, so the ordinary reconciliation pass -- the
    one that already runs whenever the SSH config is edited outside the app --
    carries UUID, display name, group membership and metadata across.
    """
    repo, root, state = _repo(tmp_path, "Host alpha\n    HostName a.example\n")
    group = repo.create_group("Work")
    repo.assign_connection_to_group("alpha", group.id)
    repo.update_connection_metadata("alpha", {"tags": ["prod"]})
    repo.update_connection(
        "alpha",
        {
            "nickname": "alpha",
            "hostname": "a.example",
            "protocol": "ssh",
            "display_name": "Alpha Box",
        },
    )
    before = read_identity_state_v2(state)
    uuid = next(
        item.uuid for item in before.identities
        if not item.tombstone and item.projection.alias == "alpha"
    )
    _leave_orphaned_intent(
        monkeypatch, repo, nickname="b", hostname="b.example", protocol="ssh"
    )
    del repo

    # Renamed behind the app's back, so the note is unplaceable.
    root.write_text(
        root.read_text().replace("Host alpha", "Host alpha2"), encoding="utf-8"
    )
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    assert restarted._identity_state_unavailable is False
    after = read_identity_state_v2(state)
    carried = next(
        item for item in after.identities
        if not item.tombstone and item.uuid == uuid
    )
    assert carried.projection.alias == "alpha2"
    assert carried.display_name == "Alpha Box"
    assert dict(after.metadata)[uuid] == {"tags": ["prod"]}
    assert any(
        ConnectionReference(ReferenceKind.SSH_UUID, uuid) in item.members
        for item in after.groups
    )


def test_malformed_pending_intent_is_discarded_instead_of_blocking(
    tmp_path,
):
    """A note that cannot even be parsed is as unusable as an unplaceable one."""
    repo, _root, state = _repo(tmp_path, "Host old\n    HostName old.example\n")
    del repo
    identity_transaction_intent_path(state).write_text(
        "{ not a transaction", encoding="utf-8"
    )
    restarted, _root, _state = _repo(tmp_path, "Host old\n    HostName old.example\n")
    assert restarted._identity_state_unavailable is False
    assert not identity_transaction_intent_path(state).exists()
    assert restarted.create_connection(
        {"nickname": "fresh", "hostname": "fresh.example", "protocol": "ssh"}
    ).id == "fresh"
