"""Tests for the isolated UUID-owned state and v1 migration prototype."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sshpilot.core.connections.identity_reconciliation import (
    ConnectionIdentityProjection,
    IdentityFileEvidence,
    IdentityRegistryEntry,
    StaticDestinationEvidence,
    reconcile_identities,
)
from sshpilot.core.connections.identity_state_v2 import (
    ConnectionReference,
    IdentityStateV2,
    PersistedIdentity,
    ReferenceKind,
    UuidGroupState,
    migrate_v1_state,
)
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.state_file import ConnectionFileState, GroupFileState


U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def projection(alias: str, *, order: int = 0, host: str = "server.example"):
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


def test_v1_migration_preserves_app_state_and_quarantines_stale_references():
    state = ConnectionFileState(
        non_ssh_connections=({"id": "serial-1", "protocol": "serial"},),
        groups=(
            GroupFileState(
                id="work",
                name="Work",
                connection_ids=("alpha", "serial-1", "alpha", "missing"),
            ),
        ),
        root_connections=("serial-1", "alpha", "missing"),
        metadata={
            "alpha": {"pinned": True, "display_name": "Alpha server"},
            "serial-1": {"note": "console"},
            "missing": {"note": "keep for review"},
        },
    )

    migrated, report = migrate_v1_state(
        state,
        (projection("alpha"),),
        ssh_config_revision="rev-1",
        uuid_factory=iter((U1,)).__next__,
    )

    assert [identity.uuid for identity in migrated.identities] == [U1]
    assert migrated.identities[0].display_name == "Alpha server"
    assert migrated.metadata[U1] == {"pinned": True}
    assert migrated.root_connections == (
        ConnectionReference(ReferenceKind.NON_SSH_ID, "serial-1"),
        ConnectionReference(ReferenceKind.SSH_UUID, U1),
    )
    assert [member.value for member in migrated.groups[0].members] == [
        U1,
        "serial-1",
    ]
    assert migrated.non_ssh_metadata == {"serial-1": {"note": "console"}}
    assert [(item.kind, item.alias) for item in migrated.legacy_orphans] == [
        ("group_member", "missing"),
        ("root_connection", "missing"),
        ("metadata", "missing"),
    ]
    assert report.duplicate_references_deduplicated == 1
    assert report.stale_references_quarantined == 3


def test_v2_round_trip_preserves_typed_references_and_projection_evidence():
    state = IdentityStateV2(
        identities=(
            PersistedIdentity(U1, "Alpha", projection("alpha")),
        ),
        groups=(
            UuidGroupState(
                id="work",
                name="Work",
                members=(ConnectionReference(ReferenceKind.SSH_UUID, U1),),
            ),
        ),
        root_connections=(ConnectionReference(ReferenceKind.SSH_UUID, U1),),
        metadata={U1: {"pinned": True}},
        sidecar_generation=3,
        last_reconciled_ssh_revision="rev-1",
        observed_ssh_revision="rev-1",
    )

    restored = IdentityStateV2.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored == state
    assert restored.identities[0].projection.destination_anchor == ("server.example", 22)
    assert restored.to_dict()["identities"][U1]["projection"]["source"] == "/tmp/config"


def test_v2_rejects_unknown_references_and_malformed_non_ssh_records():
    base = {
        "version": 2,
        "identities": {},
        "groups": [],
        "root_connections": [],
        "metadata": {},
        "non_ssh_connections": [],
        "non_ssh_metadata": {},
        "legacy_orphans": [],
        "pending_ambiguities": [],
    }
    unknown_root = {**base, "root_connections": [{"kind": "ssh_uuid", "id": U1}]}
    with pytest.raises(ValueError, match="unknown SSH UUID"):
        IdentityStateV2.from_dict(unknown_root)

    malformed_non_ssh = {**base, "non_ssh_connections": ["not an object"]}
    with pytest.raises(TypeError, match="non-SSH connections"):
        IdentityStateV2.from_dict(malformed_non_ssh)


def test_v1_uuid_factory_collision_fails_without_partial_state():
    state = ConnectionFileState()
    with pytest.raises(ValueError, match="duplicate identity UUID"):
        migrate_v1_state(
            state,
            (projection("alpha"), projection("beta", order=1)),
            ssh_config_revision="rev-1",
            uuid_factory=lambda: U1,
        )


def test_restart_alias_rename_uses_persisted_projection_from_actual_loader(tmp_path: Path):
    config = tmp_path / "config"
    config.write_text("Host old\n    HostName server.example\n    User deploy\n", encoding="utf-8")
    old_loaded = load_ssh_configuration(config, isolated=True)
    old_projection = projection("old")
    # Use the loader record itself; the explicit helper above only supplies a
    # stable assertion for this fixture's expected evidence.
    old_projection = ConnectionIdentityProjection.from_record(
        old_loaded.connections[0], declaration_order=0
    )
    state, _ = migrate_v1_state(
        ConnectionFileState(),
        (old_projection,),
        ssh_config_revision=old_loaded.root_revision,
        uuid_factory=iter((U1,)).__next__,
    )
    serialized = json.loads(json.dumps(state.to_dict()))
    restarted = IdentityStateV2.from_dict(serialized)

    config.write_text(
        "Host new\n    HostName server.example\n    User deploy\n", encoding="utf-8"
    )
    new_loaded = load_ssh_configuration(config, isolated=True)
    new_projection = ConnectionIdentityProjection.from_record(
        new_loaded.connections[0], declaration_order=0
    )

    assert restarted.identities[0].projection.alias == "old"
    assert new_projection.alias == "new"
    assert new_loaded.root_revision != old_loaded.root_revision
    result = reconcile_identities(
        tuple(
            IdentityRegistryEntry(
                identity.uuid,
                identity.projection,
                identity.display_name,
                identity.tombstone,
            )
            for identity in restarted.identities
        ),
        (new_projection,),
        uuid_factory=lambda: U2,
    )
    assert [(item.old.uuid, item.new_projection.alias) for item in result.matched] == [
        (U1, "new")
    ]
    assert restarted.identities[0].projection.destination_anchor == new_projection.destination_anchor


def test_restart_ambiguous_rename_keeps_all_candidates_unassigned(tmp_path: Path):
    config = tmp_path / "config"
    config.write_text(
        "Host old-a\n    HostName server.example\n    User deploy\n\n"
        "Host old-b\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(config, isolated=True)
    projections = tuple(
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(loaded.connections)
    )
    state, _ = migrate_v1_state(
        ConnectionFileState(),
        projections,
        ssh_config_revision=loaded.root_revision,
        uuid_factory=iter((U1, U2)).__next__,
    )
    restarted = IdentityStateV2.from_dict(json.loads(json.dumps(state.to_dict())))
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
            IdentityRegistryEntry(
                identity.uuid,
                identity.projection,
                identity.display_name,
                identity.tombstone,
            )
            for identity in restarted.identities
        ),
        new_projections,
        uuid_factory=lambda: "33333333-3333-4333-8333-333333333333",
    )
    assert result.matched == ()
    assert result.created == ()
    assert result.deleted == ()
    assert len(result.ambiguous) == 1
    assert {entry.uuid for entry in result.ambiguous[0].old} == {U1, U2}
    assert {item.alias for item in result.ambiguous[0].new} == {"new-a", "new-b"}


def test_tombstoned_identity_is_not_resurrected_by_alias_reuse():
    from sshpilot.core.connections.identity_reconciliation import (
        IdentityRegistryEntry,
        reconcile_identities,
    )

    tombstone = IdentityRegistryEntry(U1, projection("old", host="server-a"), tombstone=True)
    result = reconcile_identities(
        (tombstone,),
        (projection("old", host="server-b"),),
        uuid_factory=lambda: U2,
    )
    assert result.matched == ()
    assert result.deleted == ()
    assert [entry.uuid for entry in result.created] == [U2]


def test_legacy_empty_identity_evidence_is_weaker_not_explicit_none():
    from sshpilot.core.connections.identity_reconciliation import IdentityFileEvidenceMode, IdentityRegistry

    payload = {
        "version": 1,
        "identities": [
            {
                "uuid": U1,
                "display_name": "Alpha",
                "projection": {
                    "alias": "alpha",
                    "hostname": "server.example",
                    "port": 22,
                    "username": "deploy",
                    "username_literal": "deploy",
                    "username_is_explicit": True,
                    "identity_files": [],
                    "identity_file_evidence_status": "safe_static_literal",
                    "destination_evidence_status": "trustworthy",
                    "destination_evidence_reason": "explicit_static",
                },
            }
        ],
    }
    restored = IdentityRegistry.from_dict(payload)
    assert (
        restored.entries[0].projection.identity_file_evidence.mode
        is IdentityFileEvidenceMode.UNSPECIFIED
    )
