"""Adversarial tests for the backend connection-identity prototype."""

from __future__ import annotations

import json
from typing import Optional, Tuple

import pytest

from sshpilot.core.connections.identity_reconciliation import (
    ConnectionIdentityProjection,
    IdentityRegistry,
    IdentityRegistryEntry,
    MatchReason,
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
    username: str = "deploy",
    identity_files: Tuple[str, ...] = (),
    order: int = 0,
    source: str = "/tmp/config",
) -> ConnectionIdentityProjection:
    return ConnectionIdentityProjection(
        alias=alias,
        hostname=hostname,
        port=port,
        username=username,
        identity_files=identity_files,
        declaration_order=order,
        source=source,
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
        (projection("new", username="root", identity_files=("b",)), MatchReason.DESTINATION_ORDER_FALLBACK),
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


@pytest.mark.parametrize(
    "old_items,new_items,expected",
    [
        ([old("a", projection("old-a", order=0))], [projection("new-a", order=0)], ["a"]),
        (
            [old("a", projection("old-a", order=0))],
            [projection("new-a", order=0), projection("new-b", order=1)],
            ["a", "created-1"],
        ),
        (
            [old("a", projection("old-a", order=0)), old("b", projection("old-b", order=1))],
            [projection("new-a", order=0)],
            ["a"],
        ),
        (
            [old("a", projection("old-a", order=0)), old("b", projection("old-b", order=1))],
            [projection("new-a", order=0), projection("new-b", order=1)],
            ["a", "b"],
        ),
        (
            [old("a", projection("old-a", order=0)), old("b", projection("old-b", order=1))],
            [projection("new-a", order=0), projection("new-b", order=1), projection("new-c", order=2)],
            ["a", "b", "created-1"],
        ),
        (
            [old("a", projection("old-a", order=0)), old("b", projection("old-b", order=1)), old("c", projection("old-c", order=2))],
            [projection("new-a", order=0), projection("new-b", order=1)],
            ["a", "b"],
        ),
    ],
)
def test_destination_collision_shapes_are_one_to_one(old_items, new_items, expected):
    result = reconcile(old_items, new_items)
    assert [item.old.uuid for item in result.matched] + [
        item.uuid for item in result.created
    ] == expected
    assert len({item.old.uuid for item in result.matched}) == len(result.matched)
    assert len({item.uuid for item in result.created}) == len(result.created)


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


def test_identical_collision_candidates_use_declaration_order_as_deterministic_fallback():
    result = reconcile(
        [
            old("u1", projection("old-one", username="one", order=0)),
            old("u2", projection("old-two", username="two", order=1)),
        ],
        [
            projection("new-one", username="new-one", order=0),
            projection("new-two", username="new-two", order=1),
        ],
    )
    assert [(item.old.uuid, item.new_projection.alias) for item in result.matched] == [
        ("u1", "new-one"),
        ("u2", "new-two"),
    ]
    assert all(
        item.reason is MatchReason.DESTINATION_ORDER_FALLBACK
        for item in result.matched
    )


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
    assert [item.destination_anchor for item in projections] == [
        ("example.com", 22),
        ("example.com", 22),
    ]
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


def test_external_repository_reload_currently_does_not_reconcile_alias_rename(tmp_path):
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
    assert [item.id for item in snapshot.connections] == ["new"]
    assert snapshot.groups[0].connection_ids == ()
    assert snapshot.metadata == ()
    # The dormant alias-keyed state remains on disk; reload did not apply the
    # raw-editor-only rename heuristic.
    assert json.loads(state.read_text(encoding="utf-8"))["metadata"] == {
        "old": {"pinned": True}
    }


def test_daemon_reload_path_reports_external_rename_as_delete_create_currently(tmp_path):
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
    assert [item.id for item in result.deleted] == ["old"]
    assert [item.id for item in result.created] == ["new"]


def test_registry_from_actual_records_is_restartable(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host old\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    first = load_ssh_configuration(root, isolated=True)
    registry = registry_from_records(first.connections, uuid_factory=iter(["u1"]).__next__)
    root.write_text(
        "Host new\n    HostName server.example\n    User deploy\n",
        encoding="utf-8",
    )
    second = load_ssh_configuration(root, isolated=True)
    projections = [
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(second.connections)
    ]
    result = reconcile(registry.entries, projections)
    assert result.matched[0].old.uuid == "u1"
    assert result.matched[0].reason is MatchReason.DESTINATION_USER_IDENTITY
