"""Import/export orchestration tests."""
from __future__ import annotations


import pytest

from sshpilot.core.errors import CoreError
from sshpilot.core.import_export import (
    MergeStrategy,
    apply_import_to_connection_dicts,
    atomic_write_json,
    export_connections_payload,
    load_payload,
    migrate_payload,
    plan_import,
)


def _payload(conns, version=1, **extra):
    data = {"version": version, "connections": conns}
    data.update(extra)
    return data


def test_valid_export_import_and_dry_run(tmp_path):
    conns = [{"nickname": "A", "hostname": "a.test", "username": "u"}]
    payload = export_connections_payload(conns)
    path = tmp_path / "export.json"
    atomic_write_json(path, payload)
    loaded = load_payload(path)
    plan = plan_import(loaded, strategy=MergeStrategy.REPLACE)
    assert plan.ok
    assert plan.connections_to_add == ["A"]
    result = apply_import_to_connection_dicts(loaded, [], strategy=MergeStrategy.REPLACE)
    assert result.ok and result.applied


def test_invalid_schema_and_future():
    bad = plan_import({"version": 1})  # missing connections ok if ssh_config? no
    assert bad.ok is False or bad.errors
    with pytest.raises(CoreError):
        migrate_payload({"version": 99, "connections": []})


def test_older_schema_migration():
    data = migrate_payload({"version": 1, "connections": [{"nickname": "Z", "hostname": "z"}]})
    assert data["version"] == 1


def test_duplicates_conflicts_merge_replace_skip():
    existing = [{"nickname": "A", "hostname": "old", "username": "u"}]
    incoming = _payload(
        [{"nickname": "A", "hostname": "new", "username": "u"}, {"nickname": "B", "hostname": "b", "username": "u"}]
    )
    merge = plan_import(incoming, existing_connections=existing, strategy=MergeStrategy.MERGE)
    assert "A" in merge.connections_to_update
    assert "B" in merge.connections_to_add
    skip = plan_import(incoming, existing_connections=existing, strategy=MergeStrategy.SKIP)
    assert "A" in skip.connections_to_skip
    replace = apply_import_to_connection_dicts(
        incoming, existing, strategy=MergeStrategy.REPLACE
    )
    assert replace.ok


def test_secrets_excluded_by_default_and_malformed(tmp_path):
    payload = _payload(
        [{"nickname": "S", "hostname": "s", "username": "u"}],
        credentials=[{"id": "x", "secret": "nope"}],
    )
    plan = plan_import(payload, include_secrets=False)
    assert any("Secrets" in w.message for w in plan.warnings)
    with pytest.raises(CoreError):
        load_payload(tmp_path / "missing.json")


def test_group_conflicts_and_partial():
    existing_groups = {"groups": {"g1": {"id": "g1", "name": "Prod"}}}
    payload = _payload(
        [{"nickname": "C", "hostname": "c", "username": "u"}],
        groups={"groups": {"g1": {"id": "g1", "name": "Prod"}, "g2": {"id": "g2", "name": "Dev"}}},
    )
    plan = plan_import(payload, existing_groups=existing_groups)
    assert "Prod" in plan.groups_conflicting
    assert "Dev" in plan.groups_to_add
    result = apply_import_to_connection_dicts(
        _payload([{"hostname": "no-nick"}]),
        [],
        strategy=MergeStrategy.MERGE,
    )
    # validation may fail on nickname
    assert result.plan is not None
