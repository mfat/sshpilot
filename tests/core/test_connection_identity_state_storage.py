"""Production filesystem tests for v2 identity state and transaction intents."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest
import sshpilot.core.connections.state_file as state_file_module

from sshpilot.core.connections.identity_reconciliation import (
    ConnectionIdentityProjection,
    IdentityFileEvidence,
    IdentityRegistryEntry,
    StaticDestinationEvidence,
    reconcile_identities,
)
from sshpilot.core.connections.identity_state_v2 import (
    ConnectionReference,
    IdentityRecoveryAction,
    IdentityStateV2,
    IdentityTransactionIntent,
    PendingAmbiguity,
    PersistedIdentity,
    ReferenceKind,
    _projection_from_dict,
    _projection_to_dict,
    classify_identity_transaction_recovery,
)
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.state_file import (
    ConnectionFileState,
    ConnectionStateFileKind,
    GroupFileState,
    clear_pending_identity_transaction,
    identity_transaction_intent_path,
    migrate_connection_state_v1_to_v2,
    probe_connection_state_file,
    read_identity_state_v2,
    read_pending_identity_transaction,
    recover_pending_identity_transaction,
    write_connection_state,
    write_identity_state_v2,
    write_pending_identity_transaction,
)
from sshpilot.core.errors import CoreError


U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def _projection(alias: str, *, order: int = 0, host: str = "server.example"):
    return ConnectionIdentityProjection(
        alias=alias,
        hostname=host,
        port=22,
        username="deploy",
        identity_files=(),
        declaration_order=order,
        source="/tmp/config",
        destination_evidence=StaticDestinationEvidence.trustworthy(host, 22),
        username_literal="deploy",
        username_is_explicit=True,
        identity_file_evidence=IdentityFileEvidence.unspecified(),
    )


def _state(*, generation: int = 0, revision: str = "rev-old", alias: str = "old"):
    return IdentityStateV2(
        identities=(PersistedIdentity(U1, "Production", _projection(alias)),),
        root_connections=(ConnectionReference(ReferenceKind.SSH_UUID, U1),),
        sidecar_generation=generation,
        last_reconciled_ssh_revision=revision,
        observed_ssh_revision=revision,
    )


def _intent(base: IdentityStateV2, target: IdentityStateV2) -> IdentityTransactionIntent:
    return IdentityTransactionIntent(
        transaction_id="tx-1",
        base_ssh_revision="rev-old",
        target_ssh_revision="rev-new",
        base_sidecar_generation=base.sidecar_generation,
        operation_label="test transaction",
        target_state=target,
    )


def test_v2_reader_writer_round_trip_and_deterministic_bytes(tmp_path: Path):
    path = tmp_path / "connections.json"
    state = _state()
    write_identity_state_v2(path, state)
    first = path.read_bytes()
    assert read_identity_state_v2(path) == state
    write_identity_state_v2(path, state)
    assert path.read_bytes() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert probe_connection_state_file(path) is ConnectionStateFileKind.V2


def test_v2_missing_is_explicit_and_malformed_never_becomes_empty(tmp_path: Path):
    path = tmp_path / "connections.json"
    assert read_identity_state_v2(path) is None
    path.write_text("{broken", encoding="utf-8")
    assert probe_connection_state_file(path) is ConnectionStateFileKind.CORRUPT
    with pytest.raises(CoreError):
        read_identity_state_v2(path)


def test_main_writer_rejects_oversized_v2_before_touching_target(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "connections.json"
    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", 1)
    with pytest.raises(CoreError):
        write_identity_state_v2(path, _state())
    assert not path.exists()

    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", 16 * 1024 * 1024)
    write_identity_state_v2(path, _state())
    before = path.read_bytes()
    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", 1)
    with pytest.raises(CoreError):
        write_identity_state_v2(path, _state(generation=1))
    assert path.read_bytes() == before


def test_shared_v1_writer_rejects_oversized_serialization(tmp_path: Path, monkeypatch):
    path = tmp_path / "connections.json"
    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", 1)
    with pytest.raises(CoreError):
        write_connection_state(path, ConnectionFileState())
    assert not path.exists()


@pytest.mark.parametrize(
    "writer",
    [
        lambda path: path.write_bytes(b"\xff"),
        lambda path: path.write_text("[]", encoding="utf-8"),
        lambda path: path.write_text(json.dumps({"version": 999}), encoding="utf-8"),
        lambda path: path.write_text(json.dumps({"version": 1}), encoding="utf-8"),
    ],
)
def test_v2_reader_rejects_non_v2_or_invalid_files(tmp_path: Path, writer):
    path = tmp_path / "connections.json"
    writer(path)
    kind = probe_connection_state_file(path)
    if "999" in path.read_text(encoding="utf-8", errors="ignore"):
        assert kind is ConnectionStateFileKind.UNSUPPORTED
    else:
        assert kind is ConnectionStateFileKind.CORRUPT
    with pytest.raises(CoreError):
        read_identity_state_v2(path)


def test_v2_symlink_is_rejected_for_read_and_write(tmp_path: Path):
    target = tmp_path / "target.json"
    write_identity_state_v2(target, _state())
    link = tmp_path / "connections.json"
    link.symlink_to(target)
    with pytest.raises(CoreError):
        read_identity_state_v2(link)
    with pytest.raises(CoreError):
        write_identity_state_v2(link, _state())


def test_v1_to_v2_migration_is_atomic_and_idempotent(tmp_path: Path):
    path = tmp_path / "connections.json"
    write_connection_state(
        path,
        ConnectionFileState(
            groups=(GroupFileState("work", "Work", connection_ids=("old",)),),
            metadata={"old": {"pinned": True}},
        ),
    )
    before = path.read_bytes()
    migrated, report = migrate_connection_state_v1_to_v2(
        path,
        (_projection("old"),),
        ssh_config_revision="rev-old",
        uuid_factory=lambda: U1,
    )
    assert report.identities_created == 1
    assert migrated.identities[0].uuid == U1
    assert migrated.metadata == {U1: {"pinned": True}}
    assert probe_connection_state_file(path) is ConnectionStateFileKind.V2
    restored = read_identity_state_v2(path)
    assert restored == migrated
    with pytest.raises(CoreError) as excinfo:
        migrate_connection_state_v1_to_v2(
            path,
            (_projection("old"),),
            ssh_config_revision="rev-old",
            uuid_factory=lambda: U2,
        )
    assert excinfo.value.diagnostic_category == "invalid_state"
    assert before != path.read_bytes()
    assert read_identity_state_v2(path).identities[0].uuid == U1


@pytest.mark.parametrize("revision", [None, ""])
def test_v1_to_v2_migration_requires_revision_and_preserves_v1(
    tmp_path: Path, revision
):
    path = tmp_path / "connections.json"
    write_connection_state(path, ConnectionFileState())
    before = path.read_bytes()
    with pytest.raises(ValueError, match="revision"):
        migrate_connection_state_v1_to_v2(
            path,
            (_projection("old"),),
            ssh_config_revision=revision,
            uuid_factory=lambda: U1,
        )
    assert path.read_bytes() == before


def test_failed_migration_keeps_valid_v1_bytes(tmp_path: Path):
    path = tmp_path / "connections.json"
    write_connection_state(path, ConnectionFileState())
    before = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate identity UUID"):
        migrate_connection_state_v1_to_v2(
            path,
            (_projection("a"), _projection("b", order=1)),
            ssh_config_revision="rev-old",
            uuid_factory=lambda: U1,
        )
    assert path.read_bytes() == before
    assert probe_connection_state_file(path) is ConnectionStateFileKind.V1


def test_real_loader_migration_persists_projection_and_restart_rename(tmp_path: Path):
    config = tmp_path / "ssh_config"
    config.write_text(
        "Host old-prod\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(config, isolated=True)
    record = loaded.connections[0]
    projection = ConnectionIdentityProjection.from_record(record, declaration_order=0)
    path = tmp_path / "connections.json"
    write_connection_state(path, ConnectionFileState())
    state, _ = migrate_connection_state_v1_to_v2(
        path,
        (projection,),
        ssh_config_revision=loaded.root_revision,
        uuid_factory=lambda: U1,
    )
    assert state.last_reconciled_ssh_revision == loaded.root_revision
    assert state.observed_ssh_revision == loaded.root_revision
    assert state.identities[0].display_name == "old-prod"

    config.write_text(
        "Host prod\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    restarted = read_identity_state_v2(path)
    new_loaded = load_ssh_configuration(config, isolated=True)
    new_projection = ConnectionIdentityProjection.from_record(
        new_loaded.connections[0], declaration_order=0
    )
    result = reconcile_identities(
        tuple(
            IdentityRegistryEntry(
                item.uuid,
                item.projection,
                item.display_name,
                item.tombstone,
            )
            for item in restarted.identities
        ),
        (new_projection,),
        uuid_factory=lambda: U2,
    )
    assert [(match.old.uuid, match.new_projection.alias) for match in result.matched] == [
        (U1, "prod")
    ]


def test_real_loader_restart_ambiguity_survives_disk_serialization(tmp_path: Path):
    config = tmp_path / "ssh_config"
    config.write_text(
        "Host old-a\n    HostName server.example\n    User deploy\n\n"
        "Host old-b\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    old_loaded = load_ssh_configuration(config, isolated=True)
    old_projections = tuple(
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(old_loaded.connections)
    )
    path = tmp_path / "connections.json"
    write_connection_state(path, ConnectionFileState())
    migrate_connection_state_v1_to_v2(
        path,
        old_projections,
        ssh_config_revision=old_loaded.root_revision,
        uuid_factory=iter((U1, U2)).__next__,
    )
    state = read_identity_state_v2(path)
    config.write_text(
        "Host new-b\n    HostName server.example\n    User deploy\n\n"
        "Host new-a\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    new_loaded = load_ssh_configuration(config, isolated=True)
    new_projections = tuple(
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(new_loaded.connections)
    )
    result = reconcile_identities(
        tuple(
            IdentityRegistryEntry(item.uuid, item.projection, item.display_name)
            for item in state.identities
        ),
        new_projections,
        uuid_factory=lambda: U2,
    )
    assert result.matched == ()
    assert result.created == ()
    assert result.deleted == ()
    assert len(result.ambiguous) == 1


def test_intent_round_trip_and_revision_generation_invariants(tmp_path: Path):
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    intent = _intent(base, target)
    path = tmp_path / "connections.json.pending"
    write_pending_identity_transaction(path, intent)
    assert read_pending_identity_transaction(path) == intent
    assert identity_transaction_intent_path(tmp_path / "connections.json") == path
    with pytest.raises(ValueError, match="base generation plus one"):
        IdentityTransactionIntent(
            transaction_id="tx",
            base_ssh_revision="old",
            target_ssh_revision="new",
            base_sidecar_generation=4,
            operation_label="bad",
            target_state=target,
        )
    with pytest.raises(ValueError, match="target SSH revision"):
        IdentityTransactionIntent(
            transaction_id="tx",
            base_ssh_revision="old",
            target_ssh_revision="new",
            base_sidecar_generation=0,
            operation_label="bad",
            target_state=replace(target, last_reconciled_ssh_revision="old"),
        )


def test_intent_limits_cover_envelope_and_nested_target(tmp_path: Path, monkeypatch):
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    path = tmp_path / "connections.json.pending"
    monkeypatch.setattr(state_file_module, "_MAX_IDENTITY_INTENT_BYTES", 1)
    with pytest.raises(CoreError):
        write_pending_identity_transaction(path, _intent(base, target))
    assert not path.exists()

    monkeypatch.setattr(state_file_module, "_MAX_IDENTITY_INTENT_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", 1)
    with pytest.raises(CoreError):
        write_pending_identity_transaction(path, _intent(base, target))
    assert not path.exists()


def test_transaction_kind_semantics_are_explicit():
    base = _state()
    safe_target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    assert _intent(base, safe_target).operation_kind == "normal"

    with pytest.raises(ValueError, match="target SSH revision"):
        IdentityTransactionIntent(
            transaction_id="tx",
            base_ssh_revision="rev-old",
            target_ssh_revision="rev-new",
            base_sidecar_generation=0,
            operation_label="stale observed",
            target_state=replace(safe_target, observed_ssh_revision="rev-old"),
        )
    pending = replace(
        _state(generation=1, revision="rev-new", alias="old"),
        last_reconciled_ssh_revision="rev-old",
        observed_ssh_revision="rev-new",
        pending_ambiguities=(
            PendingAmbiguity("rev-new", (U1,), (_projection("new"),)),
        ),
    )
    pending_intent = IdentityTransactionIntent(
        transaction_id="tx-pending",
        base_ssh_revision="rev-old",
        target_ssh_revision="rev-new",
        base_sidecar_generation=0,
        operation_label="pending SSH commit",
        operation_kind="pending_ambiguity",
        target_state=pending,
    )
    restored = IdentityTransactionIntent.from_dict(
        json.loads(json.dumps(pending_intent.to_dict()))
    )
    assert restored.operation_kind == "pending_ambiguity"

    with pytest.raises(ValueError, match="pending ambiguities"):
        IdentityTransactionIntent(
            transaction_id="tx-empty",
            base_ssh_revision="rev-old",
            target_ssh_revision="rev-new",
            base_sidecar_generation=0,
            operation_label="missing pending",
            operation_kind="pending_ambiguity",
            target_state=safe_target,
        )
    with pytest.raises(ValueError, match="transaction kind"):
        IdentityTransactionIntent(
            transaction_id="tx-old-kind",
            base_ssh_revision="rev-old",
            target_ssh_revision="rev-new",
            base_sidecar_generation=0,
            operation_label="old spelling",
            operation_kind="ambiguity_resolution",
            target_state=safe_target,
        )


def test_probe_requires_structurally_complete_recognized_versions(tmp_path: Path):
    path = tmp_path / "connections.json"
    write_connection_state(path, ConnectionFileState())
    assert probe_connection_state_file(path) is ConnectionStateFileKind.V1
    write_identity_state_v2(path, _state())
    assert probe_connection_state_file(path) is ConnectionStateFileKind.V2

    for version in (1, 2):
        path.write_text(json.dumps({"version": version}), encoding="utf-8")
        assert probe_connection_state_file(path) is ConnectionStateFileKind.CORRUPT

    path.write_text(json.dumps({"version": "2"}), encoding="utf-8")
    assert probe_connection_state_file(path) is ConnectionStateFileKind.CORRUPT

    state = _state()
    payload = state.to_dict()
    payload["metadata"] = {U1: {"password": "not allowed"}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert probe_connection_state_file(path) is ConnectionStateFileKind.CORRUPT


def test_intent_target_must_fit_main_sidecar_limit(tmp_path: Path, monkeypatch):
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    path = tmp_path / "connections.json.pending"
    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", 1)
    monkeypatch.setattr(
        state_file_module, "_MAX_IDENTITY_INTENT_BYTES", 32 * 1024 * 1024
    )
    with pytest.raises(CoreError):
        write_pending_identity_transaction(path, _intent(base, target))
    assert not path.exists()


def test_v2_from_dict_requires_every_canonical_field(tmp_path: Path):
    path = tmp_path / "connections.json"
    state = _state()
    payload = state.to_dict()
    required = (
        "version",
        "sidecar_generation",
        "identities",
        "groups",
        "root_connections",
        "metadata",
        "non_ssh_connections",
        "non_ssh_metadata",
        "legacy_orphans",
        "pending_ambiguities",
        "observed_ssh_revision",
        "last_reconciled_ssh_revision",
    )
    for field in required:
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises((TypeError, ValueError)):
            IdentityStateV2.from_dict(missing)
        path.write_text(json.dumps(missing), encoding="utf-8")
        assert probe_connection_state_file(path) is ConnectionStateFileKind.CORRUPT
        with pytest.raises(CoreError):
            read_identity_state_v2(path)


def test_intent_from_dict_requires_every_canonical_field(tmp_path: Path):
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    payload = _intent(base, target).to_dict()
    required = (
        "version",
        "transaction_id",
        "base_ssh_revision",
        "target_ssh_revision",
        "base_sidecar_generation",
        "operation_label",
        "operation_kind",
        "target_state",
    )
    path = tmp_path / "connections.json.pending"
    for field in required:
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises((TypeError, ValueError)):
            IdentityTransactionIntent.from_dict(missing)
        path.write_text(json.dumps(missing), encoding="utf-8")
        with pytest.raises(CoreError):
            read_pending_identity_transaction(path)


def test_external_oversized_target_intent_is_rejected_on_read_and_recovery(
    tmp_path: Path, monkeypatch
):
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        metadata={U1: {"notes": "x" * 2000}},
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    intent = _intent(base, target)
    sidecar = tmp_path / "connections.json"
    intent_path = identity_transaction_intent_path(sidecar)
    write_identity_state_v2(sidecar, base)
    write_bytes = json.dumps(intent.to_dict(), indent=2, sort_keys=True).encode()
    intent_path.write_bytes(write_bytes)
    before_sidecar = sidecar.read_bytes()
    before_intent = intent_path.read_bytes()
    base_size = len(before_sidecar)
    monkeypatch.setattr(state_file_module, "_MAX_STATE_BYTES", base_size + 1)
    assert len(json.dumps(target.to_dict(), sort_keys=True).encode()) > base_size + 1
    with pytest.raises(CoreError):
        read_pending_identity_transaction(intent_path)
    with pytest.raises(CoreError):
        recover_pending_identity_transaction(
            sidecar,
            intent_path=intent_path,
            actual_ssh_revision="rev-new",
        )
    assert sidecar.read_bytes() == before_sidecar
    assert intent_path.read_bytes() == before_intent


def test_recovery_crash_window_matrix(tmp_path: Path):
    sidecar = tmp_path / "connections.json"
    intent_path = identity_transaction_intent_path(sidecar)
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    intent = _intent(base, target)
    write_identity_state_v2(sidecar, base)

    write_pending_identity_transaction(intent_path, intent)
    assert recover_pending_identity_transaction(
        sidecar, actual_ssh_revision="rev-old"
    ).action is IdentityRecoveryAction.ABORT_BASE
    assert not intent_path.exists()
    assert read_identity_state_v2(sidecar) == base

    write_pending_identity_transaction(intent_path, intent)
    assert recover_pending_identity_transaction(
        sidecar, actual_ssh_revision="rev-new"
    ).action is IdentityRecoveryAction.FINALIZE_TARGET
    assert read_identity_state_v2(sidecar) == target
    assert not intent_path.exists()

    write_pending_identity_transaction(intent_path, intent)
    assert recover_pending_identity_transaction(
        sidecar, actual_ssh_revision="rev-new"
    ).action is IdentityRecoveryAction.FINALIZE_TARGET
    assert not intent_path.exists()

    write_identity_state_v2(sidecar, base)
    write_pending_identity_transaction(intent_path, intent)
    decision = recover_pending_identity_transaction(
        sidecar, actual_ssh_revision="rev-external"
    )
    assert decision.action is IdentityRecoveryAction.REQUIRES_RECONCILIATION
    assert intent_path.exists()

    write_identity_state_v2(sidecar, replace(base, sidecar_generation=2))
    decision = classify_identity_transaction_recovery(
        read_identity_state_v2(sidecar), intent, "rev-old"
    )
    assert decision.action is IdentityRecoveryAction.STALE_INTENT

    assert classify_identity_transaction_recovery(
        base, intent, None
    ).action is IdentityRecoveryAction.DEFERRED


def test_intent_corruption_and_symlink_are_not_no_pending(tmp_path: Path):
    sidecar = tmp_path / "connections.json"
    intent = identity_transaction_intent_path(sidecar)
    intent.write_text("{broken", encoding="utf-8")
    with pytest.raises(CoreError):
        read_pending_identity_transaction(intent)
    intent.unlink()
    target = tmp_path / "real.pending"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "connections.json.pending"
    link.symlink_to(target)
    with pytest.raises(CoreError):
        clear_pending_identity_transaction(link)
    with pytest.raises(CoreError):
        write_pending_identity_transaction(link, _intent(_state(), replace(
            _state(generation=1, revision="rev-new", alias="new"),
            last_reconciled_ssh_revision="rev-new",
            observed_ssh_revision="rev-new",
        )))


def test_corrupt_sidecar_is_not_replaced_during_recovery(tmp_path: Path):
    sidecar = tmp_path / "connections.json"
    intent_path = identity_transaction_intent_path(sidecar)
    sidecar.write_text("{broken", encoding="utf-8")
    base = _state()
    target = replace(
        _state(generation=1, revision="rev-new", alias="new"),
        last_reconciled_ssh_revision="rev-new",
        observed_ssh_revision="rev-new",
    )
    write_pending_identity_transaction(intent_path, _intent(base, target))
    with pytest.raises(CoreError):
        recover_pending_identity_transaction(sidecar, actual_ssh_revision="rev-new")
    assert sidecar.read_text(encoding="utf-8") == "{broken"
    assert intent_path.exists()


def test_trustworthy_projection_round_trips_through_real_v2_serialization():
    original = ConnectionIdentityProjection(
        alias="prod",
        hostname="server-a",
        port=2222,
        username="deploy",
        destination_evidence=StaticDestinationEvidence.trustworthy("server-a", 2222),
        username_literal="deploy",
        username_is_explicit=True,
        identity_file_evidence=IdentityFileEvidence.unspecified(),
    )
    restored = _projection_from_dict(_projection_to_dict(original))
    assert restored == original
    assert restored.destination_anchor == original.destination_anchor


def test_unsafe_metadata_cannot_be_written_to_v2_or_intent(tmp_path: Path):
    with pytest.raises((TypeError, ValueError)):
        IdentityStateV2(
            identities=(PersistedIdentity(U1, "bad", _projection("old")),),
            metadata={U1: {"password": "secret"}},
        )
