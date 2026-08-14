"""Production repository coverage for the UUID-owned sidecar adapter."""

from __future__ import annotations

from pathlib import Path

from sshpilot.core.connections.identity_state_v2 import IdentityStateV2
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.connections.state_file import read_identity_state_v2
from sshpilot.core.connections.state_file import identity_transaction_intent_path
from sshpilot.api.models.connections import SaveSshConfigTextRequest
from sshpilot.api.models.connections import CreateConnectionRequest, UpdateConnectionRequest


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


def test_startup_migrates_v1_and_keeps_uuid_across_restart(tmp_path):
    repo, root, state = _repo(tmp_path, "Host old\n    HostName server.example\n    User deploy\n")
    first = read_identity_state_v2(state)
    assert isinstance(first, IdentityStateV2)
    uuid = first.identities[0].uuid
    del repo
    restarted, _root, _state = _repo(tmp_path, root.read_text())
    second = read_identity_state_v2(state)
    assert second.identities[0].uuid == uuid
    assert restarted.snapshot().connections[0].ssh_alias == "old"


def test_external_safe_rename_uses_persisted_projection(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host old\n    HostName server.example\n    User deploy\n"
    )
    uuid = read_identity_state_v2(state).identities[0].uuid
    root.write_text("Host new\n    HostName server.example\n    User deploy\n")
    assert repo.reload().connections[0].ssh_alias == "new"
    assert read_identity_state_v2(state).identities[0].uuid == uuid


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
    assert {item.ssh_alias for item in snapshot.connections} == {"new-a", "new-b"}
    assert not persisted.pending_ambiguities[0].new_projections == ()
    assert {item.uuid for item in persisted.identities} == old_uuids
    assert persisted.last_reconciled_ssh_revision != persisted.observed_ssh_revision


def test_managed_alias_and_destination_change_keeps_uuid(tmp_path):
    repo, root, state = _repo(
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


def test_display_name_is_sidecar_only_and_public_id_is_uuid(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n    User deploy\n"
    )
    before = root.read_bytes()
    identity = read_identity_state_v2(state).identities[0]
    repo.set_display_name(identity.uuid, "Production Server / تهران")
    summary = repo.snapshot().connections[0]
    assert summary.id == identity.uuid
    assert summary.ssh_alias == "prod"
    assert summary.display_name == "Production Server / تهران"
    assert root.read_bytes() == before


def test_prepared_ssh_mutation_does_not_write_before_commit(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )
    before_config = root.read_bytes()
    before_state = state.read_bytes()
    prepared = repo._ssh_store.prepare_update(
        "prod",
        {"nickname": "prod2", "hostname": "new.example", "protocol": "ssh"},
        expected_generation=0,
    )
    assert root.read_bytes() == before_config
    assert state.read_bytes() == before_state
    assert not identity_transaction_intent_path(state).exists()
    assert prepared.target_revision != prepared.base_revision


def test_crash_after_intent_before_ssh_recovers_base(tmp_path):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )

    class Crash(BaseException):
        pass

    original = repo._ssh_store.commit_prepared

    def crash(_prepared):
        assert identity_transaction_intent_path(state).exists()
        raise Crash()

    repo._ssh_store.commit_prepared = crash
    try:
        with __import__("pytest").raises(Crash):
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
    assert restarted.snapshot().connections[0].ssh_alias == "prod"
    assert not identity_transaction_intent_path(state).exists()


def test_crash_after_ssh_before_sidecar_recovers_target(tmp_path, monkeypatch):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )

    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    monkeypatch.setattr(repo, "_persist_state_file_locked", crash)
    with __import__("pytest").raises(Crash):
        repo.update_connection(
            "prod",
            {"nickname": "prod2", "hostname": "new.example", "protocol": "ssh"},
            expected_generation=0,
        )
    assert root.read_text(encoding="utf-8").startswith("Host prod2")
    assert identity_transaction_intent_path(state).exists()
    old_uuid = read_identity_state_v2(state).identities[0].uuid

    restarted, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))
    recovered = read_identity_state_v2(state)
    assert restarted.snapshot().connections[0].ssh_alias == "prod2"
    assert recovered.identities[0].uuid == old_uuid
    assert not identity_transaction_intent_path(state).exists()


def test_crash_after_sidecar_before_intent_clear_is_idempotent(tmp_path, monkeypatch):
    repo, root, state = _repo(
        tmp_path, "Host prod\n    HostName server.example\n"
    )

    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    monkeypatch.setattr(repo, "_finish_identity_intent_locked", crash)
    with __import__("pytest").raises(Crash):
        repo.update_connection(
            "prod",
            {"nickname": "prod2", "hostname": "new.example", "protocol": "ssh"},
            expected_generation=0,
        )
    target_uuid = read_identity_state_v2(state).identities[0].uuid
    assert root.read_text(encoding="utf-8").startswith("Host prod2")
    assert identity_transaction_intent_path(state).exists()

    restarted, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))
    assert read_identity_state_v2(state).identities[0].uuid == target_uuid
    assert restarted.snapshot().connections[0].ssh_alias == "prod2"
    assert not identity_transaction_intent_path(state).exists()


def test_display_name_is_independent_for_create_and_update(tmp_path):
    repo, root, state = _repo(tmp_path, "")
    created = repo.create_connection(
        {
            "nickname": "prod",
            "hostname": "server.example",
            "protocol": "ssh",
            "display_name": "EU / Database #2 — تهران",
        }
    )
    summary = repo.snapshot().connections[0]
    assert summary.id != summary.ssh_alias
    assert summary.ssh_alias == "prod"
    assert summary.display_name == "EU / Database #2 — تهران"
    before = root.read_bytes()
    repo.set_display_name(created.id, "Production Server")
    assert root.read_bytes() == before
    assert repo.snapshot().connections[0].display_name == "Production Server"

    # The public request models permit ordinary human labels, while alias
    # validation remains separate and SSH-shaped.
    assert UpdateConnectionRequest(display_name="John's Mac")
    assert CreateConnectionRequest(
        nickname="db",
        hostname="db.example",
        display_name="Database / 東京",
    )
