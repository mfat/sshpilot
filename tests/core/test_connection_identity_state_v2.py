"""Tests for the isolated UUID-owned state and v1 migration prototype."""

from __future__ import annotations

import json
from dataclasses import replace
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
    AmbiguityResolution,
    ConnectionReference,
    IdentityStateV2,
    LegacyOrphan,
    PendingAmbiguity,
    PersistedIdentity,
    ReferenceKind,
    UuidGroupState,
    migrate_v1_state,
    validate_ambiguity_resolution,
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


def state_with_identities(
    identities=(U1,),
    *,
    tombstones=(),
    groups=(),
    root=None,
    non_ssh=(),
    pending=(),
    observed_revision="rev-1",
):
    persisted = tuple(
        PersistedIdentity(
            identity_uuid,
            identity_uuid,
            projection(identity_uuid, order=index),
            tombstone=identity_uuid in tombstones,
        )
        for index, identity_uuid in enumerate(identities)
    )
    group_values = tuple(groups)
    grouped = {member for group in group_values for member in group.members}
    if root is None:
        root = tuple(
            ConnectionReference(ReferenceKind.SSH_UUID, identity_uuid)
            for identity_uuid in identities
            if identity_uuid not in tombstones
            and ConnectionReference(ReferenceKind.SSH_UUID, identity_uuid) not in grouped
        )
    return IdentityStateV2(
        identities=persisted,
        groups=group_values,
        root_connections=tuple(root),
        non_ssh_connections=tuple(non_ssh),
        pending_ambiguities=tuple(pending),
        observed_ssh_revision=observed_revision,
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
    assert migrated.root_connections == ()
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
    assert report.duplicate_references_deduplicated == 3
    assert report.stale_references_quarantined == 3
    assert report.placement_conflicts_resolved == 2


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
        root_connections=(),
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


def test_pending_ambiguity_requires_non_empty_unique_candidates():
    with pytest.raises(ValueError, match="old UUID"):
        PendingAmbiguity("rev-1", (), (projection("new-a"),))
    with pytest.raises(ValueError, match="new projections"):
        PendingAmbiguity("rev-1", (U1,), ())
    with pytest.raises(ValueError, match="unique tuple"):
        PendingAmbiguity("rev-1", (U1, U1), (projection("new-a"),))
    with pytest.raises(ValueError, match="aliases must be unique"):
        PendingAmbiguity("rev-1", (U1,), (projection("new-a"), projection("new-a")))


def test_pending_ambiguity_requires_active_known_old_uuids_and_unique_global_sets():
    pending = PendingAmbiguity("rev-1", (U1,), (projection("new-a"),))
    payload = state_with_identities(pending=(pending,)).to_dict()
    payload["pending_ambiguities"][0]["old_uuids"] = [U2]
    with pytest.raises(ValueError, match="active SSH UUID"):
        IdentityStateV2.from_dict(payload)

    tombstone_state = state_with_identities(tombstones=(U1,))
    tombstone_payload = tombstone_state.to_dict()
    tombstone_payload["pending_ambiguities"] = [pending.to_dict()]
    with pytest.raises(ValueError, match="active SSH UUID"):
        IdentityStateV2.from_dict(tombstone_payload)

    with pytest.raises(ValueError, match="one ambiguity only"):
        state_with_identities(
            identities=(U1, U2),
            pending=(
                PendingAmbiguity("rev-1", (U1,), (projection("new-a"),)),
                PendingAmbiguity("rev-1", (U1,), (projection("new-b"),)),
            ),
        )

    with pytest.raises(ValueError, match="one ambiguity only"):
        state_with_identities(
            identities=(U1, U2),
            pending=(
                PendingAmbiguity("rev-1", (U1,), (projection("new-a"),)),
                PendingAmbiguity("rev-1", (U2,), (projection("new-a"),)),
            ),
        )


def test_tombstones_are_not_placement_references_but_may_retain_metadata():
    tombstone_ref = ConnectionReference(ReferenceKind.SSH_UUID, U1)
    with pytest.raises(ValueError, match="tombstoned identities"):
        IdentityStateV2(
            identities=(PersistedIdentity(U1, "Alpha", projection("alpha"), True, 1),),
            root_connections=(tombstone_ref,),
        )
    with pytest.raises(ValueError, match="tombstoned identities"):
        IdentityStateV2(
            identities=(PersistedIdentity(U1, "Alpha", projection("alpha"), True, 1),),
            groups=(UuidGroupState("work", "Work", (tombstone_ref,)),),
        )
    state = IdentityStateV2(
        identities=(PersistedIdentity(U1, "Alpha", projection("alpha"), True, 1),),
        metadata={U1: {"historical": True}},
    )
    assert state.metadata[U1] == {"historical": True}


def test_v2_rejects_group_root_conflict_and_invalid_tombstone_placement():
    reference = ConnectionReference(ReferenceKind.SSH_UUID, U1)
    with pytest.raises(ValueError, match="grouped connections"):
        IdentityStateV2(
            identities=(PersistedIdentity(U1, "Alpha", projection("alpha")),),
            groups=(UuidGroupState("work", "Work", (reference,)),),
            root_connections=(reference,),
        )


def test_v1_migration_appends_unplaced_ssh_and_non_ssh_connections_to_root():
    state = ConnectionFileState(
        non_ssh_connections=({"id": "serial-1", "protocol": "serial"},),
        groups=(GroupFileState("work", "Work", connection_ids=("alpha",)),),
    )
    migrated, report = migrate_v1_state(
        state,
        (projection("alpha"), projection("beta", order=1)),
        ssh_config_revision="rev-1",
        uuid_factory=iter((U1, U2)).__next__,
    )
    assert migrated.root_connections == (
        ConnectionReference(ReferenceKind.SSH_UUID, U2),
        ConnectionReference(ReferenceKind.NON_SSH_ID, "serial-1"),
    )
    assert report.unplaced_connections_appended == 2


def test_legacy_orphan_round_trip_preserves_ordering_context():
    orphan = LegacyOrphan(
        "group_member",
        "missing",
        group_id="work",
        original_index=1,
        original_container_length=3,
    )
    restored = LegacyOrphan.from_dict(json.loads(json.dumps(orphan.to_dict())))
    assert restored == orphan
    assert restored.original_index == 1
    assert restored.original_container_length == 3


def test_v1_namespace_collision_quarantines_group_root_and_metadata_references():
    state = ConnectionFileState(
        non_ssh_connections=({"id": "console", "protocol": "serial"},),
        groups=(GroupFileState("work", "Work", connection_ids=("console",)),),
        root_connections=("console",),
        metadata={"console": {"pinned": True}},
    )
    migrated, report = migrate_v1_state(
        state,
        (projection("console"),),
        ssh_config_revision="rev-1",
        uuid_factory=iter((U1,)).__next__,
    )
    assert migrated.groups[0].members == ()
    assert [reference.kind for reference in migrated.root_connections] == [
        ReferenceKind.SSH_UUID,
        ReferenceKind.NON_SSH_ID,
    ]
    assert migrated.metadata == {}
    assert len(migrated.legacy_orphans) == 3
    assert {orphan.reason for orphan in migrated.legacy_orphans} == {
        "namespace_collision"
    }
    assert report.namespace_collisions_quarantined == 1


def test_metadata_and_orphan_values_must_be_json_objects():
    payload = state_with_identities().to_dict()
    payload["metadata"][U1] = ["not", "an", "object"]
    with pytest.raises(TypeError, match="metadata values"):
        IdentityStateV2.from_dict(payload)

    payload = state_with_identities(non_ssh=({"id": "serial-1"},), root=(
        ConnectionReference(ReferenceKind.SSH_UUID, U1),
        ConnectionReference(ReferenceKind.NON_SSH_ID, "serial-1"),
    )).to_dict()
    payload["non_ssh_metadata"]["serial-1"] = ["not", "an", "object"]
    with pytest.raises(TypeError, match="non-SSH metadata"):
        IdentityStateV2.from_dict(payload)

    with pytest.raises(TypeError, match="legacy orphan values"):
        LegacyOrphan.from_dict({"kind": "metadata", "alias": "x", "values": []})


def test_group_parent_must_exist_and_must_not_cycle():
    with pytest.raises(ValueError, match="group parent"):
        IdentityStateV2(
            groups=(UuidGroupState("child", "Child", parent_id="missing"),),
        )
    with pytest.raises(ValueError, match="cycles"):
        IdentityStateV2(
            groups=(
                UuidGroupState("a", "A", parent_id="b"),
                UuidGroupState("b", "B", parent_id="a"),
            ),
        )


def test_ambiguity_resolution_requires_complete_revision_guarded_accounting():
    pending = PendingAmbiguity(
        "rev-2",
        (U1, U2),
        (projection("new-a"), projection("new-b", order=1)),
    )
    state = state_with_identities(
        identities=(U1, U2),
        pending=(pending,),
        observed_revision="rev-2",
    )
    resolution = AmbiguityResolution(
        expected_ssh_revision="rev-2",
        expected_sidecar_generation=0,
        old_to_new=((U1, "new-a"),),
        explicit_creates=("new-b",),
        explicit_deletes=(U2,),
    )
    assert validate_ambiguity_resolution(state, resolution) == pending

    with pytest.raises(ValueError, match="stale SSH revision"):
        validate_ambiguity_resolution(
            state,
            replace(resolution, expected_ssh_revision="rev-3"),
        )
    with pytest.raises(ValueError, match="account for exactly one"):
        validate_ambiguity_resolution(
            state,
            replace(resolution, explicit_creates=()),
        )


def test_revision_change_can_replace_pending_ambiguity_without_reusing_it():
    pending = PendingAmbiguity("rev-1", (U1,), (projection("new-a"),))
    state = state_with_identities(pending=(pending,), observed_revision="rev-1")
    with pytest.raises(ValueError, match="must equal observed"):
        replace(state, observed_ssh_revision="rev-2")
    refreshed = replace(
        state,
        pending_ambiguities=(),
        observed_ssh_revision="rev-2",
    )
    assert refreshed.pending_ambiguities == ()
    assert refreshed.observed_ssh_revision == "rev-2"


def test_pending_ambiguity_revision_must_match_observed_revision():
    pending = PendingAmbiguity("rev-1", (U1,), (projection("new-a"),))
    state = state_with_identities(pending=(pending,), observed_revision="rev-1")
    payload = state.to_dict()
    payload["pending_ambiguities"][0]["ssh_config_revision"] = "rev-2"
    with pytest.raises(ValueError, match="must equal observed"):
        IdentityStateV2.from_dict(payload)

    with pytest.raises(ValueError, match="require an observed"):
        state_with_identities(pending=(pending,), observed_revision=None)
    with pytest.raises(ValueError, match="non-empty"):
        state_with_identities(pending=(pending,), observed_revision="")


def test_optional_revision_fields_reject_empty_strings_but_can_differ():
    state = state_with_identities(observed_revision="rev-2")
    valid = replace(state, last_reconciled_ssh_revision="rev-1")
    assert valid.last_reconciled_ssh_revision == "rev-1"
    with pytest.raises(ValueError, match="last-reconciled"):
        replace(state, last_reconciled_ssh_revision="")
    with pytest.raises(ValueError, match="observed"):
        replace(state, observed_ssh_revision="")


def test_group_member_orphan_requires_canonical_position_fields():
    valid = LegacyOrphan(
        "group_member",
        "missing",
        group_id="work",
        original_index=1,
        original_container_length=3,
    )
    assert LegacyOrphan.from_dict(valid.to_dict()) == valid
    for field in ("group_id", "original_index", "original_container_length"):
        payload = valid.to_dict()
        payload.pop(field)
        with pytest.raises((TypeError, ValueError)):
            LegacyOrphan.from_dict(payload)
    with pytest.raises(ValueError, match="inside"):
        LegacyOrphan("group_member", "missing", "work", {}, 3, 3)


def test_root_orphan_requires_canonical_position_without_group():
    valid = LegacyOrphan(
        "root_connection",
        "missing",
        original_index=1,
        original_container_length=3,
    )
    assert LegacyOrphan.from_dict(valid.to_dict()) == valid
    for field in ("original_index", "original_container_length"):
        payload = valid.to_dict()
        payload.pop(field)
        with pytest.raises((TypeError, ValueError)):
            LegacyOrphan.from_dict(payload)
    payload = valid.to_dict()
    payload["group_id"] = "work"
    with pytest.raises(ValueError, match="group id"):
        LegacyOrphan.from_dict(payload)


def test_metadata_orphan_has_no_placement_coordinates():
    valid = LegacyOrphan("metadata", "missing", values={"note": "review"})
    assert LegacyOrphan.from_dict(valid.to_dict()) == valid
    for field, value in (("group_id", "work"), ("original_index", 0), ("original_container_length", 1)):
        payload = valid.to_dict()
        payload[field] = value
        with pytest.raises(ValueError, match="metadata orphan"):
            LegacyOrphan.from_dict(payload)


def test_migration_orphans_have_canonical_ordering_context():
    state = ConnectionFileState(
        groups=(GroupFileState("work", "Work", connection_ids=("a", "missing", "b")),),
        root_connections=("a", "missing-root", "b"),
    )
    migrated, _ = migrate_v1_state(
        state,
        (projection("a"), projection("b", order=1)),
        ssh_config_revision="rev-1",
        uuid_factory=iter((U1, U2)).__next__,
    )
    group_orphan = next(item for item in migrated.legacy_orphans if item.kind == "group_member")
    root_orphan = next(item for item in migrated.legacy_orphans if item.kind == "root_connection")
    assert (group_orphan.group_id, group_orphan.original_index, group_orphan.original_container_length) == (
        "work",
        1,
        3,
    )
    assert (root_orphan.group_id, root_orphan.original_index, root_orphan.original_container_length) == (
        None,
        1,
        3,
    )
    restored = IdentityStateV2.from_dict(json.loads(json.dumps(migrated.to_dict())))
    assert restored.legacy_orphans == migrated.legacy_orphans


def test_namespace_collision_placement_orphans_keep_coordinates():
    state = ConnectionFileState(
        non_ssh_connections=({"id": "console"},),
        groups=(GroupFileState("work", "Work", connection_ids=("console",)),),
        root_connections=("console",),
    )
    migrated, _ = migrate_v1_state(
        state,
        (projection("console"),),
        ssh_config_revision="rev-1",
        uuid_factory=iter((U1,)).__next__,
    )
    group_orphan, root_orphan = [
        item for item in migrated.legacy_orphans if item.kind != "metadata"
    ]
    assert (group_orphan.group_id, group_orphan.original_index, group_orphan.original_container_length) == (
        "work",
        0,
        1,
    )
    assert (root_orphan.group_id, root_orphan.original_index, root_orphan.original_container_length) == (
        None,
        0,
        1,
    )


def test_direct_malformed_group_construction_has_deliberate_type_error():
    with pytest.raises(TypeError, match="groups"):
        IdentityStateV2(groups=(object(),))
