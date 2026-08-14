"""Production repository coverage for internal UUID identity with alias API IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from sshpilot.api.models.connections import SaveSshConfigTextRequest
from sshpilot.core.connections.identity_state_v2 import IdentityStateV2
from sshpilot.core.connections.identity_state_v2 import ConnectionReference
from sshpilot.core.connections.identity_state_v2 import ReferenceKind
from sshpilot.core.connections.repository import ConnectionRepository
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


def test_corrupt_pending_intent_drops_previously_trusted_identity(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )
    repo.set_display_name("prod", "Production")
    pending = identity_transaction_intent_path(state)
    pending.write_text("{broken", encoding="utf-8")
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    assert restarted.snapshot().connections[0].display_name == "prod"
    with pytest.raises(CoreError) as exc:
        restarted.set_display_name("prod", "Should not write")
    assert exc.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR
    assert pending.read_text(encoding="utf-8") == "{broken"


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


def test_dangling_pending_intent_symlink_enters_degraded_state(tmp_path):
    repo, _root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )
    intent_path = identity_transaction_intent_path(state)
    intent_path.symlink_to(tmp_path / "missing-intent")

    restarted, _root, _state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )

    assert restarted.snapshot().connections[0].id == "prod"
    assert intent_path.is_symlink()
    with pytest.raises(CoreError) as exc_info:
        restarted.set_display_name("prod", "Unavailable")
    assert exc_info.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR


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
