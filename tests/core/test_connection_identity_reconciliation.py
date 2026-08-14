"""Adversarial tests for the backend connection-identity prototype."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional, Tuple

import pytest

from sshpilot.core.connections.identity_reconciliation import (
    ConnectionIdentityProjection,
    DestinationAnchor,
    DestinationEvidenceReason,
    DestinationEvidenceStatus,
    IdentityFileEvidenceMode,
    IdentityFileEvidence,
    IdentityFileEvidenceStatus,
    IdentityRegistry,
    IdentityRegistryEntry,
    MatchReason,
    StaticDestinationEvidence,
    apply_reconciliation,
    reconcile_identities,
    registry_from_records,
)
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.daemon.config_reload import AuthoritativeConfigurationBackend


def projection(
    alias: str,
    *,
    hostname: Optional[str] = "server.example",
    port: Optional[int] = 22,
    username: Optional[str] = "deploy",
    identity_files: Tuple[str, ...] = (),
    order: int = 0,
    source: str = "/tmp/config",
) -> ConnectionIdentityProjection:
    if hostname is not None and port is not None:
        destination_evidence = StaticDestinationEvidence.trustworthy(hostname, port)
    else:
        destination_evidence = StaticDestinationEvidence.unavailable(
            DestinationEvidenceReason.MISSING_HOSTNAME
        )
    username_is_explicit = username is not None
    if any("$" in value or "%" in value.replace("%%", "") for value in identity_files):
        identity_evidence = IdentityFileEvidence.dynamic("test dynamic expression")
    else:
        identity_evidence = IdentityFileEvidence.safe(identity_files)
    return ConnectionIdentityProjection(
        alias=alias,
        hostname=hostname,
        port=port,
        username=username or "",
        identity_files=identity_files,
        declaration_order=order,
        source=source,
        destination_evidence=destination_evidence,
        username_literal=username,
        username_is_explicit=username_is_explicit,
        identity_file_evidence=identity_evidence,
    )


def old(uuid: str, item: ConnectionIdentityProjection, name: str = "Saved"):
    return IdentityRegistryEntry(uuid=uuid, projection=item, display_name=name)


def reconcile(old_entries, new_projections, ids=("created-1", "created-2", "created-3")):
    values = iter(ids)
    return reconcile_identities(
        old_entries,
        new_projections,
        uuid_factory=lambda: next(values),
    )


def test_exact_alias_has_priority_over_every_other_change():
    result = reconcile(
        [old("u1", projection("prod", identity_files=("a",)))],
        [
            projection(
                "prod",
                hostname="new.example",
                port=2222,
                username="root",
                identity_files=("b",),
            )
        ],
    )

    assert [(item.old.uuid, item.reason) for item in result.matched] == [
        ("u1", MatchReason.EXACT_ALIAS)
    ]
    assert not result.created and not result.deleted


@pytest.mark.parametrize(
    "new, reason",
    [
        (projection("new", username="deploy", identity_files=("a",)), MatchReason.DESTINATION_USER_IDENTITY),
        (projection("new", username="deploy", identity_files=("b",)), MatchReason.DESTINATION_USER),
        (projection("new", username="root", identity_files=("b",)), MatchReason.DESTINATION_UNIQUE_REMAINDER),
    ],
)
def test_destination_anchor_preserves_uuid_when_alias_changes(new, reason):
    result = reconcile([old("u1", projection("old", identity_files=("a",)))], [new])
    assert len(result.matched) == 1
    assert result.matched[0].old.uuid == "u1"
    assert result.matched[0].reason is reason
    assert result.matched[0].old.display_name == "Saved"


def test_missing_hostname_is_not_a_rename_anchor():
    result = reconcile(
        [old("u1", projection("old", hostname=None))],
        [projection("new", hostname=None)],
    )
    assert result.matched == ()
    assert [item.uuid for item in result.deleted] == ["u1"]
    assert [item.uuid for item in result.created] == ["created-1"]


def test_changed_destination_is_create_delete():
    result = reconcile(
        [old("u1", projection("old", hostname="one.example"))],
        [projection("new", hostname="two.example")],
    )
    assert not result.matched
    assert [item.uuid for item in result.deleted] == ["u1"]
    assert [item.uuid for item in result.created] == ["created-1"]


def test_port_22_normalization_and_invalid_port_policy():
    same = reconcile(
        [old("u1", projection("old", port=22))],
        [projection("new", port=22)],
    )
    invalid = reconcile(
        [old("u1", projection("old", port=None))],
        [projection("new", port=22)],
    )
    assert same.matched[0].old.uuid == "u1"
    assert invalid.deleted[0].uuid == "u1"
    assert invalid.created[0].uuid == "created-1"


def test_projection_requires_explicit_destination_evidence():
    projection_without_evidence = ConnectionIdentityProjection(
        alias="old",
        hostname="server.example",
        port=22,
    )
    result = reconcile(
        [old("u1", projection_without_evidence)],
        [projection("new")],
    )
    assert projection_without_evidence.destination_anchor is None
    assert not result.matched
    assert [item.uuid for item in result.created] == ["created-1"]


def test_uuid_factory_collision_with_existing_or_new_uuid_fails_explicitly():
    with pytest.raises(ValueError, match="duplicate identity UUID"):
        reconcile(
            [old("created-1", projection("old", hostname="old.example"))],
            [projection("new", hostname="new.example"), projection("new-2", hostname="two.example")],
            ids=("created-1",),
        )
    with pytest.raises(ValueError, match="duplicate identity UUID"):
        reconcile(
            [],
            [projection("new-a", order=0), projection("new-b", order=1)],
            ids=("same", "same"),
        )


@pytest.mark.parametrize(
    "old_count,new_count",
    [(1, 2), (2, 1), (2, 2), (3, 3)],
)
def test_non_unique_destination_remainders_are_ambiguous(old_count, new_count):
    old_items = [
        old(f"old-{index}", projection(f"old-{index}", order=index))
        for index in range(old_count)
    ]
    new_items = [
        projection(f"new-{index}", order=index) for index in range(new_count)
    ]
    result = reconcile(old_items, new_items)
    assert not result.matched
    assert not result.created
    assert not result.deleted
    assert len(result.ambiguous) == 1
    assert [entry.uuid for entry in result.ambiguous[0].old] == [
        f"old-{index}" for index in range(old_count)
    ]
    assert [item.alias for item in result.ambiguous[0].new] == [
        f"new-{index}" for index in range(new_count)
    ]


def test_unique_destination_remainder_is_not_an_order_fallback():
    result = reconcile(
        [old("u1", projection("old", username=None))],
        [projection("new", username=None)],
    )
    assert result.matched[0].reason is MatchReason.DESTINATION_UNIQUE_REMAINDER


def test_collision_passes_consume_user_and_identity_before_order():
    result = reconcile(
        [
            old("root", projection("old-root", username="root", identity_files=("a",), order=0)),
            old("deploy", projection("old-deploy", username="deploy", identity_files=("b",), order=1)),
        ],
        [
            projection("new-deploy", username="deploy", identity_files=("b",), order=0),
            projection("new-root", username="root", identity_files=("a",), order=1),
        ],
    )
    assert [(item.old.uuid, item.new_projection.alias, item.reason) for item in result.matched] == [
        ("deploy", "new-deploy", MatchReason.DESTINATION_USER_IDENTITY),
        ("root", "new-root", MatchReason.DESTINATION_USER_IDENTITY),
    ]


def test_three_by_three_collision_is_consuming_and_deterministic():
    old_items = [
        old("u1", projection("old-1", username="one", identity_files=("a",), order=0)),
        old("u2", projection("old-2", username="two", identity_files=("b",), order=1)),
        old("u3", projection("old-3", username="three", identity_files=("c",), order=2)),
    ]
    new_items = [
        projection("new-3", username="three", identity_files=("c",), order=0),
        projection("new-1", username="one", identity_files=("a",), order=1),
        projection("new-2", username="two", identity_files=("b",), order=2),
    ]
    result = reconcile(old_items, new_items)
    assert [(item.old.uuid, item.new_projection.alias) for item in result.matched] == [
        ("u3", "new-3"),
        ("u1", "new-1"),
        ("u2", "new-2"),
    ]
    assert len({item.old.uuid for item in result.matched}) == 3


def test_collision_matching_is_invariant_to_input_insertion_order():
    old_items = [
        old("u1", projection("old-1", username="one", order=0)),
        old("u2", projection("old-2", username="two", order=1)),
        old("u3", projection("old-3", username="three", order=2)),
    ]
    new_items = [
        projection("new-1", username="one", order=0),
        projection("new-2", username="two", order=1),
        projection("new-3", username="three", order=2),
    ]
    first = reconcile(old_items, new_items)
    second = reconcile(list(reversed(old_items)), list(reversed(new_items)))
    assert [
        (item.old.uuid, item.new_projection.alias, item.reason)
        for item in first.matched
    ] == [
        (item.old.uuid, item.new_projection.alias, item.reason)
        for item in second.matched
    ]


def test_indistinguishable_collision_candidates_are_ambiguous_not_order_paired():
    result = reconcile(
        [
            old("u1", projection("old-one", username="deploy", order=0)),
            old("u2", projection("old-two", username="deploy", order=1)),
        ],
        [
            projection("new-one", username="deploy", order=0),
            projection("new-two", username="deploy", order=1),
        ],
    )
    assert result.matched == ()
    assert result.created == ()
    assert result.deleted == ()
    assert [entry.uuid for entry in result.ambiguous[0].old] == ["u1", "u2"]
    assert [item.alias for item in result.ambiguous[0].new] == ["new-one", "new-two"]


def test_exact_alias_survives_collision_and_cannot_be_stolen():
    result = reconcile(
        [
            old("survivor", projection("keep", order=0)),
            old("renamed", projection("old", order=1)),
        ],
        [projection("keep", order=1), projection("new", order=0)],
    )
    assert [(item.old.uuid, item.new_projection.alias) for item in result.matched] == [
        ("renamed", "new"),
        ("survivor", "keep"),
    ]
    assert any(item.reason is MatchReason.EXACT_ALIAS for item in result.matched)


@pytest.mark.parametrize(
    "left,right",
    [
        ("example.com", "EXAMPLE.com"),
        ("example.com", "example.com."),
        ("2001:db8::1", "2001:0db8:0:0:0:0:0:1"),
        ("server", "server.example.com"),
        ("%h", "server"),
    ],
)
def test_hostname_comparison_is_literal(left, right):
    result = reconcile(
        [old("u1", projection("old", hostname=left))],
        [projection("new", hostname=right)],
    )
    assert not result.matched


def test_source_and_unrelated_projection_metadata_are_not_identity():
    result = reconcile(
        [old("u1", projection("old", source="one.conf"))],
        [projection("new", source="another.conf")],
    )
    assert result.matched[0].old.uuid == "u1"


def test_tombstones_never_resurrect():
    deleted = old("old", projection("gone"))
    tombstone = IdentityRegistryEntry(
        uuid=deleted.uuid,
        projection=deleted.projection,
        display_name=deleted.display_name,
        tombstone=True,
    )
    result = reconcile([tombstone], [projection("new")])
    assert result.matched == ()
    assert result.deleted == ()
    assert result.created[0].uuid == "created-1"


def test_registry_serialization_and_restart_are_idempotent():
    registry = IdentityRegistry(
        entries=(old("u1", projection("old"), "Production"),)
    )
    restored = IdentityRegistry.from_dict(json.loads(json.dumps(registry.to_dict())))
    result = reconcile(
        restored.entries,
        [projection("new")],
    )
    next_registry = apply_reconciliation(result)
    again = reconcile(next_registry.entries, [projection("new")], ids=("unused",))
    assert next_registry.entries[0].uuid == "u1"
    assert next_registry.entries[0].display_name == "Production"
    assert again.matched[0].reason is MatchReason.EXACT_ALIAS
    assert not again.created and not again.deleted


def test_identityfile_semantic_modes_round_trip():
    entries = (
        old("u-none", _identity_projection("none", IdentityFileEvidenceMode.EXPLICIT_NONE)),
        old("u-unspecified", _identity_projection("unspecified", IdentityFileEvidenceMode.UNSPECIFIED)),
        old("u-files", _identity_projection("files", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a", "b"))),
        old("u-dynamic", _identity_projection("dynamic", IdentityFileEvidenceMode.DYNAMIC)),
    )
    restored = IdentityRegistry.from_dict(
        json.loads(json.dumps(IdentityRegistry(entries=entries).to_dict()))
    )
    assert [
        entry.projection.identity_file_evidence.mode for entry in restored.entries
    ] == [
        IdentityFileEvidenceMode.EXPLICIT_NONE,
        IdentityFileEvidenceMode.UNSPECIFIED,
        IdentityFileEvidenceMode.EXPLICIT_FILES,
        IdentityFileEvidenceMode.DYNAMIC,
    ]


def test_legacy_prototype_empty_safe_evidence_restores_conservatively():
    entries = (
        old("u-none", _identity_projection("none", IdentityFileEvidenceMode.EXPLICIT_NONE)),
        old("u-files", _identity_projection("files", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a",))),
    )
    payload = json.loads(json.dumps(IdentityRegistry(entries=entries).to_dict()))
    for raw in payload["identities"]:
        raw["projection"].pop("identity_file_evidence_mode")
    restored = IdentityRegistry.from_dict(payload)
    assert restored.entries[0].projection.identity_file_evidence.mode is IdentityFileEvidenceMode.UNSPECIFIED
    assert restored.entries[1].projection.identity_file_evidence.mode is IdentityFileEvidenceMode.EXPLICIT_FILES


@pytest.mark.parametrize(
    "mutator",
    [
        lambda projection: projection.update(
            {"destination_evidence_status": "trustworthy", "hostname": "", "port": 22}
        ),
        lambda projection: projection.update(
            {"destination_evidence_status": "trustworthy", "hostname": "server.example", "port": 0}
        ),
    ],
)
def test_malformed_destination_registry_evidence_is_rejected(mutator):
    payload = json.loads(
        json.dumps(
            IdentityRegistry(entries=(old("u1", projection("old")),)).to_dict()
        )
    )
    mutator(payload["identities"][0]["projection"])
    with pytest.raises((TypeError, ValueError)):
        IdentityRegistry.from_dict(payload)


def test_unavailable_destination_evidence_cannot_carry_anchor():
    with pytest.raises(ValueError, match="cannot carry an anchor"):
        StaticDestinationEvidence(
            DestinationEvidenceStatus.UNAVAILABLE,
            DestinationEvidenceReason.NOT_PROVIDED,
            DestinationAnchor("server.example", 22),
        )


@pytest.mark.parametrize(
    "mode,values",
    [
        (IdentityFileEvidenceMode.UNSPECIFIED, ("a",)),
        (IdentityFileEvidenceMode.EXPLICIT_NONE, ("a",)),
        (IdentityFileEvidenceMode.EXPLICIT_FILES, ()),
        (IdentityFileEvidenceMode.EXPLICIT_FILES, ("none",)),
        (IdentityFileEvidenceMode.DYNAMIC, ("a",)),
    ],
)
def test_identityfile_evidence_invariants_reject_invalid_values(mode, values):
    with pytest.raises((TypeError, ValueError)):
        IdentityFileEvidence(mode, values)


@pytest.mark.parametrize(
    "mode,values",
    [
        ("explicit_files", []),
        ("explicit_none", ["id_a"]),
        ("dynamic", ["id_a"]),
        ("unknown", []),
    ],
)
def test_malformed_identityfile_registry_evidence_is_rejected(mode, values):
    payload = json.loads(
        json.dumps(
            IdentityRegistry(
                entries=(
                    old(
                        "u1",
                        _identity_projection(
                            "old", IdentityFileEvidenceMode.EXPLICIT_FILES, ("id_a",)
                        ),
                    ),
                )
            ).to_dict()
        )
    )
    projection_payload = payload["identities"][0]["projection"]
    projection_payload["identity_file_evidence_mode"] = mode
    projection_payload["identity_file_evidence_values"] = values
    with pytest.raises((TypeError, ValueError)):
        IdentityRegistry.from_dict(payload)


@pytest.mark.parametrize(
    "hostname,port",
    [("", 22), ("server.example", 0), ("server.example", 65536), ("server.example", True)],
)
def test_destination_anchor_invariants_reject_invalid_values(hostname, port):
    with pytest.raises((TypeError, ValueError)):
        DestinationAnchor(hostname, port)


def test_actual_loader_materializes_multi_tokens_and_excludes_rules(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host foo bar\n"
        "    HostName example.com\n"
        "    IdentityFile ~/.ssh/id_a\n"
        "Host *.example.com !bad\n"
        "    HostName wildcard.example.com\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(root, isolated=True)
    assert [record.id for record in loaded.connections] == ["foo", "bar"]
    assert len(loaded.rules) == 1
    projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(loaded.connections)
    ]
    assert [item.destination_anchor for item in projections] == [None, None]
    assert all(
        item.destination_evidence.reason is DestinationEvidenceReason.INHERITED_CONFIGURATION
        for item in projections
    )
    assert projections[0].identity_files == ("~/.ssh/id_a",)


def test_nested_include_move_changes_provenance_not_identity_evidence(tmp_path):
    root = tmp_path / "config"
    first = tmp_path / "conf.d" / "first.conf"
    nested = tmp_path / "nested.conf"
    first.parent.mkdir()
    first.write_text("Include ../nested.conf\n", encoding="utf-8")
    nested.write_text("Host old\n    HostName server.example\n", encoding="utf-8")
    root.write_text("Include conf.d/first.conf\n", encoding="utf-8")
    before = load_ssh_configuration(root, isolated=True)
    before_projection = ConnectionIdentityProjection.from_record(
        before.connections[0], declaration_order=0
    )
    moved = tmp_path / "conf.d" / "moved.conf"
    moved.write_text(nested.read_text(encoding="utf-8"), encoding="utf-8")
    nested.unlink()
    first.write_text("Include moved.conf\n", encoding="utf-8")
    after = load_ssh_configuration(root, isolated=True)
    after_projection = ConnectionIdentityProjection.from_record(
        after.connections[0], declaration_order=0
    )
    result = reconcile(
        [old("u1", before_projection)],
        [after_projection],
    )
    assert before_projection.source != after_projection.source
    assert result.matched[0].reason is MatchReason.EXACT_ALIAS


@pytest.mark.parametrize(
    "match_line",
    [
        "Match user deploy",
        "Match host example.com",
        "Match originalhost old",
        "Match exec \"false\"",
        "Match localnetwork 192.0.2.0/24",
        "Match canonical",
        "Match final",
    ],
)
def test_match_directives_are_rules_and_do_not_execute_in_loader(tmp_path, match_line):
    root = tmp_path / "config"
    root.write_text(
        "Host stable\n    HostName server.example\n"
        f"{match_line}\n    User dynamic\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(root, isolated=True)
    assert [record.id for record in loaded.connections] == ["stable"]
    assert len(loaded.rules) == 1
    item = ConnectionIdentityProjection.from_record(
        loaded.connections[0], declaration_order=0
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.DYNAMIC_MATCH


def test_loader_preserves_literal_identity_evidence_and_invalid_port_is_unavailable(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host bad\n"
        "    HostName example.com\n"
        "    Port definitely-not-a-port\n"
        "    User deploy\n"
        "    IdentityFile $HOME/.ssh/id_a\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(root, isolated=True)
    record = loaded.connections[0]
    item = ConnectionIdentityProjection.from_record(record, declaration_order=0)
    assert record.port == 22  # Existing loader fallback, classified below.
    assert item.port is None
    assert item.destination_anchor is None
    assert item.username == "deploy"
    assert item.identity_files == ("$HOME/.ssh/id_a",)


def test_loader_marks_host_wildcard_inheritance_unavailable(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host *\n    HostName inherited.example\n    Port 2222\n    User wildcard\n\n"
        "Host old\n    HostName server.example\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.INHERITED_CONFIGURATION
    assert not item.username_is_explicit


def test_match_derived_port_disables_rule_two(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n    HostName server.example\n"
        "Match host old\n    Port 2222\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.DYNAMIC_MATCH


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_port_values_never_become_static_evidence(tmp_path, port):
    root = tmp_path / "config"
    root.write_text(
        f"Host old\n    HostName server.example\n    Port {port}\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.INVALID_PORT


def test_explicit_octal_text_port_and_omitted_port_normalize_to_twenty_two(tmp_path):
    omitted = tmp_path / "omitted"
    explicit = tmp_path / "explicit"
    omitted.write_text("Host old\n    HostName server.example\n", encoding="utf-8")
    explicit.write_text(
        "Host new\n    HostName server.example\n    Port 022\n",
        encoding="utf-8",
    )
    old_projection = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(omitted, isolated=True).connections[0],
        declaration_order=0,
    )
    new_projection = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(explicit, isolated=True).connections[0],
        declaration_order=0,
    )
    assert old_projection.destination_anchor == new_projection.destination_anchor == (
        "server.example",
        22,
    )


def test_missing_hostname_is_unavailable_in_actual_loader(tmp_path):
    root = tmp_path / "config"
    root.write_text("Host old\n    User deploy\n", encoding="utf-8")
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.MISSING_HOSTNAME


def test_loader_marks_repeated_concrete_host_unavailable(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n    HostName first.example\n\n"
        "Host old\n    Port 2222\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.REPEATED_HOST


def test_identityfile_none_does_not_cross_with_unspecified_after_reorder(tmp_path):
    old_root = tmp_path / "old-config"
    old_root.write_text(
        "Host old-a\n"
        "    HostName server.example\n"
        "    User deploy\n"
        "    IdentityFile none\n\n"
        "Host old-b\n"
        "    HostName server.example\n"
        "    User deploy\n",
        encoding="utf-8",
    )
    new_root = tmp_path / "new-config"
    new_root.write_text(
        "Host new-b\n"
        "    HostName server.example\n"
        "    User deploy\n\n"
        "Host new-a\n"
        "    HostName server.example\n"
        "    User deploy\n"
        "    IdentityFile none\n",
        encoding="utf-8",
    )
    old_records = load_ssh_configuration(old_root, isolated=True).connections
    new_records = load_ssh_configuration(new_root, isolated=True).connections
    old_projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(old_records)
    ]
    new_projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(new_records)
    ]

    assert old_projections[0].identity_file_evidence.mode is IdentityFileEvidenceMode.EXPLICIT_NONE
    assert old_projections[1].identity_file_evidence.mode is IdentityFileEvidenceMode.UNSPECIFIED
    assert new_projections[0].identity_file_evidence.mode is IdentityFileEvidenceMode.UNSPECIFIED
    assert new_projections[1].identity_file_evidence.mode is IdentityFileEvidenceMode.EXPLICIT_NONE

    result = reconcile(
        [old("old-a-id", old_projections[0]), old("old-b-id", old_projections[1])],
        new_projections,
    )
    assert {
        match.new_projection.alias: (match.old.uuid, match.reason)
        for match in result.matched
    } == {
        "new-a": ("old-a-id", MatchReason.DESTINATION_USER_IDENTITY),
        "new-b": ("old-b-id", MatchReason.DESTINATION_USER),
    }


def _identity_projection(
    alias: str, mode: IdentityFileEvidenceMode, values: Tuple[str, ...] = ()
) -> ConnectionIdentityProjection:
    item = projection(alias, identity_files=values)
    if mode is IdentityFileEvidenceMode.EXPLICIT_NONE:
        evidence = IdentityFileEvidence.explicit_none()
    elif mode is IdentityFileEvidenceMode.EXPLICIT_FILES:
        evidence = IdentityFileEvidence.safe(values)
    elif mode is IdentityFileEvidenceMode.DYNAMIC:
        evidence = IdentityFileEvidence.dynamic("test dynamic expression")
    else:
        evidence = IdentityFileEvidence.unspecified()
    return replace(item, identity_file_evidence=evidence)


@pytest.mark.parametrize(
    "old_mode,new_mode,expected_reason",
    [
        (
            IdentityFileEvidenceMode.EXPLICIT_NONE,
            IdentityFileEvidenceMode.EXPLICIT_NONE,
            MatchReason.DESTINATION_USER_IDENTITY,
        ),
        (
            IdentityFileEvidenceMode.EXPLICIT_NONE,
            IdentityFileEvidenceMode.UNSPECIFIED,
            MatchReason.DESTINATION_USER,
        ),
        (
            IdentityFileEvidenceMode.UNSPECIFIED,
            IdentityFileEvidenceMode.EXPLICIT_NONE,
            MatchReason.DESTINATION_USER,
        ),
        (
            IdentityFileEvidenceMode.UNSPECIFIED,
            IdentityFileEvidenceMode.UNSPECIFIED,
            MatchReason.DESTINATION_USER,
        ),
        (
            IdentityFileEvidenceMode.EXPLICIT_FILES,
            IdentityFileEvidenceMode.EXPLICIT_FILES,
            MatchReason.DESTINATION_USER_IDENTITY,
        ),
        (
            IdentityFileEvidenceMode.EXPLICIT_FILES,
            IdentityFileEvidenceMode.DYNAMIC,
            MatchReason.DESTINATION_USER,
        ),
        (
            IdentityFileEvidenceMode.DYNAMIC,
            IdentityFileEvidenceMode.DYNAMIC,
            MatchReason.DESTINATION_USER,
        ),
        (
            IdentityFileEvidenceMode.DYNAMIC,
            IdentityFileEvidenceMode.EXPLICIT_FILES,
            MatchReason.DESTINATION_USER,
        ),
    ],
)
def test_identityfile_modes_control_pass_a_only_when_comparable(
    old_mode, new_mode, expected_reason
):
    old_values = ("~/.ssh/id_a",) if old_mode is IdentityFileEvidenceMode.EXPLICIT_FILES else ()
    new_values = ("~/.ssh/id_a",) if new_mode is IdentityFileEvidenceMode.EXPLICIT_FILES else ()
    result = reconcile(
        [old("u1", _identity_projection("old", old_mode, old_values))],
        [_identity_projection("new", new_mode, new_values)],
    )
    assert result.matched[0].reason is expected_reason


def test_identityfile_static_sequences_are_ordered_evidence():
    same = reconcile(
        [old("u1", _identity_projection("old", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a", "b")))],
        [_identity_projection("new", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a", "b"))],
    )
    reversed_values = reconcile(
        [old("u1", _identity_projection("old", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a", "b")))],
        [_identity_projection("new", IdentityFileEvidenceMode.EXPLICIT_FILES, ("b", "a"))],
    )
    assert same.matched[0].reason is MatchReason.DESTINATION_USER_IDENTITY
    assert reversed_values.matched[0].reason is MatchReason.DESTINATION_USER


def test_mixed_identityfile_collision_preserves_comparable_modes_after_reorder():
    old_items = [
        old("none-id", _identity_projection("old-none", IdentityFileEvidenceMode.EXPLICIT_NONE)),
        old("unspecified-id", _identity_projection("old-unspecified", IdentityFileEvidenceMode.UNSPECIFIED)),
        old("files-id", _identity_projection("old-files", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a",))),
        old("dynamic-id", _identity_projection("old-dynamic", IdentityFileEvidenceMode.DYNAMIC)),
    ]
    new_items = [
        _identity_projection("new-dynamic", IdentityFileEvidenceMode.DYNAMIC),
        _identity_projection("new-files", IdentityFileEvidenceMode.EXPLICIT_FILES, ("a",)),
        _identity_projection("new-none", IdentityFileEvidenceMode.EXPLICIT_NONE),
        _identity_projection("new-unspecified", IdentityFileEvidenceMode.UNSPECIFIED),
    ]
    first = reconcile(old_items, new_items)
    second = reconcile(list(reversed(old_items)), list(reversed(new_items)))
    mapping = {
        match.new_projection.alias: (match.old.uuid, match.reason)
        for match in first.matched
    }
    assert mapping["new-none"] == ("none-id", MatchReason.DESTINATION_USER_IDENTITY)
    assert mapping["new-files"] == ("files-id", MatchReason.DESTINATION_USER_IDENTITY)
    assert set(mapping) == {"new-none", "new-files"}
    assert [entry.uuid for entry in first.ambiguous[0].old] == [
        "dynamic-id",
        "unspecified-id",
    ]
    assert [item.alias for item in first.ambiguous[0].new] == [
        "new-dynamic",
        "new-unspecified",
    ]
    assert len({match.old.uuid for match in first.matched}) == 2
    assert {
        alias: (item.old.uuid, item.reason)
        for alias, item in (
            (match.new_projection.alias, match) for match in second.matched
        )
    } == mapping
    assert {
        entry.uuid for entry in second.ambiguous[0].old
    } == {"unspecified-id", "dynamic-id"}
    assert {item.alias for item in second.ambiguous[0].new} == {
        "new-dynamic",
        "new-unspecified",
    }


@pytest.mark.parametrize("include_position", ["before", "after"])
def test_include_position_disables_rule_two_when_loader_cannot_preserve_semantics(
    tmp_path, include_position
):
    included = tmp_path / "defaults.conf"
    included.write_text("Host *\n    Port 2222\n", encoding="utf-8")
    root = tmp_path / "config"
    if include_position == "before":
        text = "Include defaults.conf\nHost old\n    HostName server.example\n"
    else:
        text = "Host old\n    HostName server.example\nInclude defaults.conf\n"
    root.write_text(text, encoding="utf-8")
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.INCLUDE_SEMANTICS


def test_host_dependent_hostname_and_identity_file_are_not_static_evidence(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n"
        "    HostName %h.example.com\n"
        "    User deploy\n"
        "    IdentityFile ~/.ssh/%h\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is None
    assert item.destination_evidence.reason is DestinationEvidenceReason.HOST_DEPENDENT_HOSTNAME
    assert item.identity_file_evidence.status is IdentityFileEvidenceStatus.DYNAMIC


def test_environment_identity_file_is_not_safe_tie_break_evidence(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n"
        "    HostName server.example\n"
        "    IdentityFile $HOME/.ssh/id_a\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.destination_anchor is not None
    assert item.identity_file_evidence.status is IdentityFileEvidenceStatus.DYNAMIC


def test_both_omitted_users_reach_unique_remainder_only():
    result = reconcile(
        [old("u1", projection("old", username=None))],
        [projection("new", username=None)],
    )
    assert result.matched[0].reason is MatchReason.DESTINATION_UNIQUE_REMAINDER


def test_omitted_user_does_not_equal_explicit_local_username():
    result = reconcile(
        [old("u1", projection("old", username=None))],
        [projection("new", username="deploy")],
    )
    assert result.matched[0].reason is MatchReason.DESTINATION_UNIQUE_REMAINDER


def test_dynamic_identity_file_cannot_strengthen_pass_a():
    old_projection = projection("old", identity_files=("~/.ssh/%h",))
    old_projection = replace(
        old_projection,
        identity_file_evidence=IdentityFileEvidence.dynamic("%h"),
    )
    new_projection = projection("new", identity_files=("~/.ssh/%h",))
    new_projection = replace(
        new_projection,
        identity_file_evidence=IdentityFileEvidence.dynamic("%h"),
    )
    result = reconcile([old("u1", old_projection)], [new_projection])
    assert result.matched[0].reason is MatchReason.DESTINATION_USER


def _ssh_g_output(config: Path, alias: str) -> dict:
    if shutil.which("ssh") is None:
        pytest.skip("OpenSSH client is unavailable")
    completed = subprocess.run(
        ["ssh", "-G", "-F", str(config), "-o", "CanonicalizeHostname=no", alias],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    result = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in {"hostname", "port", "user", "identityfile"}:
            result.setdefault(key, []).append(value)
    return result


def test_loader_static_evidence_matches_local_openssh_oracle(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n"
        "    HostName server.example\n"
        "    Port 022\n"
        "    User deploy\n"
        "    IdentityFile ~/.ssh/id_a\n",
        encoding="utf-8",
    )
    record = load_ssh_configuration(root, isolated=True).connections[0]
    item = ConnectionIdentityProjection.from_record(record, declaration_order=0)
    oracle = _ssh_g_output(root, "old")
    assert item.destination_anchor == (oracle["hostname"][0], int(oracle["port"][0]))
    assert item.username_literal == oracle["user"][0]
    assert item.identity_file_evidence.status is IdentityFileEvidenceStatus.SAFE_STATIC_LITERAL
    assert any(value.endswith("/.ssh/id_a") for value in oracle["identityfile"])
    assert item.identity_file_evidence.values == ("~/.ssh/id_a",)


def test_include_glob_order_is_not_used_as_rule_two_evidence(tmp_path):
    include_dir = tmp_path / "conf.d"
    include_dir.mkdir()
    (include_dir / "10-first.conf").write_text(
        "Host old\n    HostName server.example\n", encoding="utf-8"
    )
    (include_dir / "20-second.conf").write_text(
        "Host other\n    HostName other.example\n", encoding="utf-8"
    )
    root = tmp_path / "config"
    root.write_text("Include conf.d/*\n", encoding="utf-8")
    first = load_ssh_configuration(root, isolated=True)
    root.write_text(
        "Include conf.d/20-second.conf\nInclude conf.d/10-first.conf\n",
        encoding="utf-8",
    )
    second = load_ssh_configuration(root, isolated=True)
    for loaded in (first, second):
        projection_by_alias = {
            record.id: ConnectionIdentityProjection.from_record(
                record, declaration_order=index
            )
            for index, record in enumerate(loaded.connections)
        }
        assert projection_by_alias["old"].destination_anchor is None
        assert (
            projection_by_alias["old"].destination_evidence.reason
            is DestinationEvidenceReason.INCLUDE_SEMANTICS
        )


def test_include_inside_host_context_is_not_assumed_static(tmp_path):
    defaults = tmp_path / "defaults.conf"
    defaults.write_text("HostName server.example\nPort 2222\n", encoding="utf-8")
    root = tmp_path / "config"
    root.write_text("Host old\n    Include defaults.conf\n", encoding="utf-8")
    loaded = load_ssh_configuration(root, isolated=True)
    # The current document/loader boundary treats Include as a structural
    # boundary and does not materialize this Host block.  Preserve that fact
    # as a parser limitation rather than manufacturing a destination anchor.
    assert loaded.connections == ()


def test_actual_loader_multi_token_partial_rename_consumes_exact_alias_first(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host foo bar\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    first = load_ssh_configuration(root, isolated=True)
    old_projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(first.connections)
    ]
    root.write_text(
        "Host foo baz\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    second = load_ssh_configuration(root, isolated=True)
    new_projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(second.connections)
    ]
    result = reconcile(
        [old("foo-id", old_projections[0]), old("bar-id", old_projections[1])],
        new_projections,
    )
    assert [(match.old.uuid, match.new_projection.alias) for match in result.matched] == [
        ("foo-id", "foo"),
        ("bar-id", "baz"),
    ]
    assert result.matched[0].reason is MatchReason.EXACT_ALIAS


def test_actual_loader_multi_token_complete_rename_is_ambiguous(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host foo bar\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    first = load_ssh_configuration(root, isolated=True)
    old_projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(first.connections)
    ]
    root.write_text(
        "Host baz qux\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    second = load_ssh_configuration(root, isolated=True)
    new_projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(second.connections)
    ]
    result = reconcile(
        [old("foo-id", old_projections[0]), old("bar-id", old_projections[1])],
        new_projections,
    )
    assert result.matched == ()
    assert result.created == ()
    assert result.deleted == ()
    assert [entry.uuid for entry in result.ambiguous[0].old] == ["foo-id", "bar-id"]
    assert [item.alias for item in result.ambiguous[0].new] == ["baz", "qux"]


@pytest.mark.parametrize(
    "identity_lines,expected",
    [
        ("    IdentityFile ~/.ssh/id_a\n    IdentityFile ~/.ssh/id_b\n", ("~/.ssh/id_a", "~/.ssh/id_b")),
        ("    IdentityFile none\n", ()),
    ],
)
def test_identity_file_order_and_none_are_stable_tie_break_evidence(
    tmp_path, identity_lines, expected
):
    root = tmp_path / "config"
    root.write_text(
        "Host one\n    HostName example.com\n" + identity_lines,
        encoding="utf-8",
    )
    record = load_ssh_configuration(root, isolated=True).connections[0]
    item = ConnectionIdentityProjection.from_record(record, declaration_order=0)
    assert item.identity_files == expected
    if expected:
        assert item.identity_file_evidence.mode is IdentityFileEvidenceMode.EXPLICIT_FILES
    else:
        assert item.identity_file_evidence.mode is IdentityFileEvidenceMode.EXPLICIT_NONE


def test_no_identityfile_directive_is_unspecified(tmp_path):
    root = tmp_path / "config"
    root.write_text("Host one\n    HostName example.com\n", encoding="utf-8")
    record = load_ssh_configuration(root, isolated=True).connections[0]
    item = ConnectionIdentityProjection.from_record(record, declaration_order=0)
    assert item.identity_file_evidence.mode is IdentityFileEvidenceMode.UNSPECIFIED


@pytest.mark.parametrize(
    "token,expected_mode",
    [
        ("%%", IdentityFileEvidenceMode.EXPLICIT_FILES),
        ("%h", IdentityFileEvidenceMode.DYNAMIC),
        ("%r", IdentityFileEvidenceMode.DYNAMIC),
        ("%p", IdentityFileEvidenceMode.DYNAMIC),
        ("%u", IdentityFileEvidenceMode.DYNAMIC),
        ("%d", IdentityFileEvidenceMode.DYNAMIC),
        ("%i", IdentityFileEvidenceMode.DYNAMIC),
        ("%l", IdentityFileEvidenceMode.DYNAMIC),
        ("%L", IdentityFileEvidenceMode.DYNAMIC),
        ("%C", IdentityFileEvidenceMode.DYNAMIC),
        ("%j", IdentityFileEvidenceMode.DYNAMIC),
        ("%k", IdentityFileEvidenceMode.DYNAMIC),
    ],
)
def test_identityfile_percent_tokens_are_classified_conservatively(
    tmp_path, token, expected_mode
):
    root = tmp_path / "config"
    root.write_text(
        "Host one\n    HostName example.com\n"
        f"    IdentityFile ~/.ssh/{token}\n",
        encoding="utf-8",
    )
    record = load_ssh_configuration(root, isolated=True).connections[0]
    item = ConnectionIdentityProjection.from_record(record, declaration_order=0)
    assert item.identity_file_evidence.mode is expected_mode


def test_tilde_identityfile_is_config_intent_evidence(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host one\n    HostName example.com\n"
        "    IdentityFile ~/.ssh/id_a\n",
        encoding="utf-8",
    )
    item = ConnectionIdentityProjection.from_record(
        load_ssh_configuration(root, isolated=True).connections[0],
        declaration_order=0,
    )
    assert item.identity_file_evidence.mode is IdentityFileEvidenceMode.EXPLICIT_FILES
    assert item.identity_file_evidence.values == ("~/.ssh/id_a",)


def test_external_repository_reload_reconciles_safe_alias_rename(tmp_path):
    root = tmp_path / "config"
    state = tmp_path / "connections.json"
    root.write_text("Host old\n    HostName server.example\n", encoding="utf-8")
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "non_ssh_connections": [],
                "groups": {
                    "groups": {
                        "prod": {
                            "id": "prod",
                            "name": "Production",
                            "order": 0,
                            "connections": ["old"],
                        }
                    },
                    "root_connections": [],
                },
                "metadata": {"old": {"pinned": True}},
            }
        ),
        encoding="utf-8",
    )
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "legacy.json",
        isolated=True,
    )
    root.write_text("Host new\n    HostName server.example\n", encoding="utf-8")
    snapshot = repo.reload()
    assert [item.ssh_alias for item in snapshot.connections] == ["new"]
    assert snapshot.metadata[0].connection_id == snapshot.connections[0].id
    assert snapshot.groups[0].connection_ids == ()
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["version"] == 2
    assert persisted["metadata"] == {
        str(snapshot.connections[0].id): {"pinned": True}
    }


def test_daemon_reload_path_preserves_external_safe_rename_identity(tmp_path):
    root = tmp_path / "config"
    root.write_text("Host old\n    HostName server.example\n", encoding="utf-8")
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "legacy.json",
        isolated=True,
    )
    backend = AuthoritativeConfigurationBackend(repo)
    root.write_text("Host new\n    HostName server.example\n", encoding="utf-8")
    result = backend.reload()
    assert result.deleted == ()
    assert result.created == ()
    assert result.updated[0].ssh_alias == "new"


def test_registry_from_actual_records_is_restartable(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    first = load_ssh_configuration(root, isolated=True)
    registry = registry_from_records(first.connections, uuid_factory=iter(["u1"]).__next__)
    restored = IdentityRegistry.from_dict(json.loads(json.dumps(registry.to_dict())))
    root.write_text(
        "Host new\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    second = load_ssh_configuration(root, isolated=True)
    projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(second.connections)
    ]
    result = reconcile(restored.entries, projections)
    assert result.matched[0].old.uuid == "u1"
    assert result.matched[0].reason is MatchReason.DESTINATION_USER


@pytest.mark.parametrize(
    "mode",
    [
        IdentityFileEvidenceMode.EXPLICIT_NONE,
        IdentityFileEvidenceMode.EXPLICIT_FILES,
        IdentityFileEvidenceMode.UNSPECIFIED,
        IdentityFileEvidenceMode.DYNAMIC,
    ],
)
def test_duplicate_identity_evidence_partition_is_ambiguous(mode):
    old_items = [
        old("u1", _identity_projection("old-a", mode, ("a",) if mode is IdentityFileEvidenceMode.EXPLICIT_FILES else ())),
        old("u2", _identity_projection("old-b", mode, ("a",) if mode is IdentityFileEvidenceMode.EXPLICIT_FILES else ())),
    ]
    new_items = [
        _identity_projection("new-a", mode, ("a",) if mode is IdentityFileEvidenceMode.EXPLICIT_FILES else ()),
        _identity_projection("new-b", mode, ("a",) if mode is IdentityFileEvidenceMode.EXPLICIT_FILES else ()),
    ]
    result = reconcile(old_items, new_items)
    assert result.matched == ()
    assert result.created == ()
    assert result.deleted == ()
    assert len(result.ambiguous) == 1


def test_unique_user_partitions_reconcile_without_declaration_order():
    old_items = [
        old("root-id", projection("old-root", username="root", order=0)),
        old("deploy-id", projection("old-deploy", username="deploy", order=1)),
        old("backup-id", projection("old-backup", username="backup", order=2)),
    ]
    new_items = [
        projection("new-backup", username="backup", order=0),
        projection("new-root", username="root", order=1),
        projection("new-deploy", username="deploy", order=2),
    ]
    result = reconcile(old_items, new_items)
    assert {
        item.new_projection.alias: item.old.uuid for item in result.matched
    } == {
        "new-root": "root-id",
        "new-deploy": "deploy-id",
        "new-backup": "backup-id",
    }
    assert all(item.reason is MatchReason.DESTINATION_USER for item in result.matched)


def test_unique_strong_match_is_consumed_before_ambiguous_remainder():
    result = reconcile(
        [
            old(
                "root-id",
                projection("old-root", username="root", identity_files=("key-a",)),
            ),
            old("deploy-a", projection("old-a", username="deploy")),
            old("deploy-b", projection("old-b", username="deploy")),
        ],
        [
            projection("new-b", username="deploy"),
            projection("new-root", username="root", identity_files=("key-a",)),
            projection("new-a", username="deploy"),
        ],
    )
    assert [
        (item.old.uuid, item.new_projection.alias, item.reason)
        for item in result.matched
    ] == [("root-id", "new-root", MatchReason.DESTINATION_USER_IDENTITY)]
    assert [entry.uuid for entry in result.ambiguous[0].old] == [
        "deploy-a",
        "deploy-b",
    ]
    assert [item.alias for item in result.ambiguous[0].new] == [
        "new-a",
        "new-b",
    ]


def test_exact_alias_is_consumed_before_ambiguous_renamed_remainder():
    result = reconcile(
        [
            old("keep-id", projection("keep", username="root")),
            old("old-a", projection("old-a", username="deploy")),
            old("old-b", projection("old-b", username="deploy")),
        ],
        [
            projection("new-b", username="deploy"),
            projection("keep", username="changed"),
            projection("new-a", username="deploy"),
        ],
    )
    assert [(item.old.uuid, item.new_projection.alias, item.reason) for item in result.matched] == [
        ("keep-id", "keep", MatchReason.EXACT_ALIAS)
    ]
    assert [entry.uuid for entry in result.ambiguous[0].old] == ["old-a", "old-b"]
    assert [item.alias for item in result.ambiguous[0].new] == ["new-a", "new-b"]


def test_ambiguous_candidates_are_not_created_or_deleted_and_cannot_be_applied():
    registry_entries = (
        old("u1", projection("old-a")),
        old("u2", projection("old-b")),
    )
    result = reconcile(
        registry_entries,
        [projection("new-a"), projection("new-b")],
    )
    assert result.ambiguous
    assert not result.created
    assert not result.deleted
    with pytest.raises(ValueError, match="unresolved ambiguous identities"):
        apply_reconciliation(result)


def test_ambiguous_result_is_semantically_invariant_to_input_order():
    old_items = [
        old("u1", projection("old-a", order=0)),
        old("u2", projection("old-b", order=1)),
        old("u3", projection("old-c", order=2)),
    ]
    new_items = [
        projection("new-a", order=0),
        projection("new-b", order=1),
        projection("new-c", order=2),
    ]
    first = reconcile(old_items, new_items)
    second = reconcile(list(reversed(old_items)), list(reversed(new_items)))

    def signature(result):
        return (
            tuple(
                sorted(
                    (item.old.uuid, item.new_projection.alias, item.reason.value)
                    for item in result.matched
                )
            ),
            tuple(
                (
                    tuple(entry.uuid for entry in group.old),
                    tuple(item.alias for item in group.new),
                )
                for group in result.ambiguous
            ),
            tuple(entry.uuid for entry in result.deleted),
            tuple(entry.uuid for entry in result.created),
        )

    assert signature(first) == signature(second)


def test_actual_loader_restart_preserves_ambiguity_after_alias_reorder(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old-a\n    HostName server.example\n    User deploy\n\n"
        "Host old-b\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    first = load_ssh_configuration(root, isolated=True)
    registry = registry_from_records(
        first.connections,
        uuid_factory=iter(["u1", "u2"]).__next__,
    )
    restored = IdentityRegistry.from_dict(json.loads(json.dumps(registry.to_dict())))

    root.write_text(
        "Host new-b\n    HostName server.example\n    User deploy\n\n"
        "Host new-a\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    second = load_ssh_configuration(root, isolated=True)
    projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(second.connections)
    ]
    result = reconcile(restored.entries, projections)
    assert not result.matched
    assert not result.created
    assert not result.deleted
    assert [entry.uuid for entry in result.ambiguous[0].old] == ["u1", "u2"]
    assert [item.alias for item in result.ambiguous[0].new] == ["new-b", "new-a"]
