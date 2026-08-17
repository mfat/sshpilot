"""Logical connection-store backup/restore tests for the headless repository.

Covers ``ConnectionRepository.snapshot_for_backup``/``restore_connection_store``
— the portable, versioned export/import used by backup/import, as distinct
from the raw ``connections.json`` sidecar (which is never restored verbatim).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepository,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402


def _repo(tmp_path, ssh_text: str = ""):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    return (
        ConnectionRepository(
            ssh_store=SshConfigStore(root),
            state_path=tmp_path / "connections.json",
            legacy_config_path=tmp_path / "config.json",
            isolated=False,
        ),
        root,
        tmp_path / "connections.json",
    )


def _telnet_data(nickname="lab-switch"):
    return {
        "nickname": nickname,
        "protocol": "telnet",
        "hostname": "10.0.0.5",
        "port": 2323,
    }


INTERNAL_BOOKKEEPING_MARKERS = (
    "uuid",
    "sidecar_generation",
    "observed_ssh_revision",
    "pending_ambiguities",
    "legacy_orphans",
)


def _contains_marker(value, markers) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if any(marker in str(key).lower() for marker in markers):
                return True
            if _contains_marker(child, markers):
                return True
    elif isinstance(value, (list, tuple)):
        for child in value:
            if _contains_marker(child, markers):
                return True
    return False


def test_snapshot_for_backup_excludes_internal_bookkeeping_and_uses_public_ids(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    repo.create_connection(_telnet_data())
    group = repo.create_group("Production", color="#ff0000")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("web",), target_group_id=group.id)
    )
    repo.update_connection_metadata("lab-switch", {"tags": ["lab"]})

    section = repo.snapshot_for_backup()

    assert section["version"] == 1
    connection_ids = {c["id"] for c in section["connections"]}
    assert connection_ids == {"web", "lab-switch"}
    group_ids = {g["id"] for g in section["groups"]}
    assert group_ids == {group.id}
    assert _contains_marker(section, INTERNAL_BOOKKEEPING_MARKERS) is False


def test_restore_connection_store_creates_non_ssh_connection_group_and_order_merge_mode(tmp_path):
    repo, root, state = _repo(tmp_path)
    section = {
        "version": 1,
        "connections": [
            {
                "id": "lab-switch",
                "protocol": "telnet",
                "nickname": "lab-switch",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            },
            {
                "id": "other",
                "protocol": "telnet",
                "nickname": "other",
                "hostname": "10.0.0.6",
                "username": "",
                "port": 23,
                "display_name": "",
            },
        ],
        "groups": [
            {
                "id": "prod",
                "name": "Production",
                "parent_id": None,
                "order": 0,
                "color": "#ff0000",
                "connection_ids": ["lab-switch"],
            }
        ],
        "root_connection_ids": ["other"],
        "metadata": [],
    }

    result = repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    assert {c.id for c in snap.connections} == {"lab-switch", "other"}
    assert len(snap.groups) == 1
    restored_group = snap.groups[0]
    assert restored_group.name == "Production"
    assert restored_group.color == "#ff0000"
    assert restored_group.connection_ids == ("lab-switch",)
    assert snap.root_connection_ids == ("other",)
    assert result.warnings == ()


def test_restore_connection_store_preserves_root_order_round_trip(tmp_path):
    source, source_root, _ = _repo(tmp_path / "source")
    for nickname in ("first", "second", "third"):
        source.create_connection(_telnet_data(nickname))
    section = source.snapshot_for_backup()

    target, target_root, _ = _repo(tmp_path / "target")
    target.restore_connection_store(section, mode="merge")

    assert target.snapshot().root_connection_ids == ("first", "second", "third")


def test_restore_connection_store_replace_mode_removes_non_ssh_connections_not_in_backup(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("keep"))
    repo.create_connection(_telnet_data("drop"))
    section = {
        "version": 1,
        "connections": [
            {
                "id": "keep",
                "protocol": "telnet",
                "nickname": "keep",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["keep"],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="replace")

    ids = {c.id for c in repo.snapshot().connections}
    assert ids == {"keep"}


def test_restore_connection_store_merge_mode_does_not_delete_existing_state(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("keep"))
    repo.create_connection(_telnet_data("also-keep"))
    section = {
        "version": 1,
        "connections": [
            {
                "id": "keep",
                "protocol": "telnet",
                "nickname": "keep",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["keep"],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    ids = {c.id for c in repo.snapshot().connections}
    assert ids == {"keep", "also-keep"}


def test_restore_connection_store_resolves_ssh_alias_after_fresh_ssh_config_write(tmp_path):
    repo, root, state = _repo(tmp_path)
    # No SSH connection exists yet when the repository is constructed. Write
    # the SSH config directly to the underlying file after construction —
    # simulating BackupManager._import_replace's SSH-tree restore, which runs
    # before the connection-store restore hook fires.
    root.write_text("Host web\n    HostName example.com\n")

    section = {
        "version": 1,
        "connections": [
            {
                "id": "web",
                "protocol": "ssh",
                "nickname": "web",
                "hostname": "example.com",
                "username": "",
                "port": 22,
                "display_name": "",
            }
        ],
        "groups": [
            {
                "id": "prod",
                "name": "Production",
                "parent_id": None,
                "order": 0,
                "color": "",
                "connection_ids": ["web"],
            }
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    assert len(snap.groups) == 1
    assert snap.groups[0].connection_ids == ("web",)


def test_restore_connection_store_is_synchronous_and_atomic_on_failure(tmp_path, monkeypatch):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("keep"))
    before = repo.snapshot()

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "_persist_state_file_locked", _boom)

    section = {
        "version": 1,
        "connections": [
            {
                "id": "new-one",
                "protocol": "telnet",
                "nickname": "new-one",
                "hostname": "10.0.0.9",
                "username": "",
                "port": 23,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["new-one"],
        "metadata": [],
    }

    with pytest.raises(RuntimeError):
        repo.restore_connection_store(section, mode="merge")

    assert repo.snapshot() == before


def test_restore_connection_store_ignores_future_version_section(tmp_path):
    repo, root, state = _repo(tmp_path)
    before = repo.snapshot()
    section = {
        "version": 999,
        "connections": [
            {
                "id": "new-one",
                "protocol": "telnet",
                "nickname": "new-one",
                "hostname": "10.0.0.9",
                "username": "",
                "port": 23,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["new-one"],
        "metadata": [],
    }

    result = repo.restore_connection_store(section, mode="merge")

    assert repo.snapshot() == before
    assert result.warnings


def test_restore_connection_store_rejects_invalid_structure_and_does_not_mutate(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("keep"))
    before = repo.snapshot()
    section = {
        "version": 1,
        "connections": [],
        "groups": [
            {
                "id": "g1",
                "name": "G",
                "parent_id": None,
                "order": 0,
                "color": "",
                # References a connection absent from "connections" — invalid
                # regardless of live repository state.
                "connection_ids": ["nonexistent"],
            }
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    with pytest.raises(ValueError):
        repo.restore_connection_store(section, mode="merge")

    assert repo.snapshot() == before


def test_restore_connection_store_reconciles_ssh_display_name(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    section = {
        "version": 1,
        "connections": [
            {
                "id": "web",
                "protocol": "ssh",
                "nickname": "web",
                "hostname": "example.com",
                "username": "",
                "port": 22,
                "display_name": "Web Server",
            }
        ],
        "groups": [],
        "root_connection_ids": ["web"],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert web.display_name == "Web Server"


def test_restore_connection_store_merge_reuses_existing_group_by_name(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("existing"))
    group = repo.create_group("Production")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("existing",), target_group_id=group.id)
    )
    repo.create_connection(_telnet_data("newcomer"))
    section = {
        "version": 1,
        "connections": [
            {
                "id": "newcomer",
                "protocol": "telnet",
                "nickname": "newcomer",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            }
        ],
        "groups": [
            {
                "id": "some-other-source-id",
                "name": "Production",
                "parent_id": None,
                "order": 0,
                "color": "",
                "connection_ids": ["newcomer"],
            }
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    production_groups = [g for g in snap.groups if g.name == "Production"]
    assert len(production_groups) == 1
    assert set(production_groups[0].connection_ids) == {"existing", "newcomer"}


def test_restore_connection_store_restores_nested_groups(tmp_path):
    repo, root, state = _repo(tmp_path)
    section = {
        "version": 1,
        "connections": [],
        "groups": [
            {"id": "parent-src", "name": "Parent", "parent_id": None,
             "order": 0, "color": "", "connection_ids": []},
            {"id": "child-src", "name": "Child", "parent_id": "parent-src",
             "order": 0, "color": "", "connection_ids": []},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    parent = next(g for g in snap.groups if g.name == "Parent")
    child = next(g for g in snap.groups if g.name == "Child")
    assert child.parent_id == parent.id


def test_restore_connection_store_replace_mode_removes_groups_not_in_backup(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("member"))
    group = repo.create_group("Old")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("member",), target_group_id=group.id)
    )
    section = {
        "version": 1,
        "connections": [
            {
                "id": "member",
                "protocol": "telnet",
                "nickname": "member",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["member"],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="replace")

    snap = repo.snapshot()
    assert snap.groups == ()
    assert snap.root_connection_ids == ("member",)


def test_restore_connection_store_replace_mode_updates_existing_group_color(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_group("Prod", color="#ff0000")
    section = {
        "version": 1,
        "connections": [],
        "groups": [
            {"id": "src-id", "name": "Prod", "parent_id": None,
             "order": 0, "color": "#0000ff", "connection_ids": []},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="replace")

    snap = repo.snapshot()
    prod = next(g for g in snap.groups if g.name == "Prod")
    assert prod.color == "#0000ff"


def test_restore_connection_store_replace_mode_clears_metadata_absent_from_backup(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("a"))
    repo.update_connection_metadata("a", {"tags": ["old"]})
    section = {
        "version": 1,
        "connections": [
            {
                "id": "a",
                "protocol": "telnet",
                "nickname": "a",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["a"],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="replace")

    snap = repo.snapshot()
    metadata = {m.connection_id: m.values for m in snap.metadata}
    assert "a" not in metadata


def test_restore_connection_store_preserves_multi_group_membership(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data("shared"))
    g1 = repo.create_group("G1")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("shared",), target_group_id=g1.id)
    )
    section = {
        "version": 1,
        "connections": [
            {
                "id": "shared",
                "protocol": "telnet",
                "nickname": "shared",
                "hostname": "10.0.0.5",
                "username": "",
                "port": 2323,
                "display_name": "",
            }
        ],
        "groups": [
            {"id": "g2-src", "name": "G2", "parent_id": None,
             "order": 0, "color": "", "connection_ids": ["shared"]},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    group_names_for_shared = {
        g.name for g in snap.groups if "shared" in g.connection_ids
    }
    assert group_names_for_shared == {"G1", "G2"}


def test_restore_connection_store_warns_on_missing_group_member_reference(tmp_path):
    repo, root, state = _repo(tmp_path)
    section = {
        "version": 1,
        "connections": [
            {
                "id": "ghost-ssh",
                "protocol": "ssh",
                "nickname": "ghost-ssh",
                "hostname": "example.com",
                "username": "",
                "port": 22,
                "display_name": "",
            }
        ],
        "groups": [
            {"id": "g1", "name": "G", "parent_id": None,
             "order": 0, "color": "", "connection_ids": ["ghost-ssh"]},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    result = repo.restore_connection_store(section, mode="merge")

    assert any("ghost-ssh" in w for w in result.warnings)
    snap = repo.snapshot()
    group = next(g for g in snap.groups if g.name == "G")
    assert group.connection_ids == ()


def test_restore_connection_store_warns_on_missing_root_reference(tmp_path):
    repo, root, state = _repo(tmp_path)
    section = {
        "version": 1,
        "connections": [
            {
                "id": "ghost-ssh",
                "protocol": "ssh",
                "nickname": "ghost-ssh",
                "hostname": "example.com",
                "username": "",
                "port": 22,
                "display_name": "",
            }
        ],
        "groups": [],
        "root_connection_ids": ["ghost-ssh"],
        "metadata": [],
    }

    result = repo.restore_connection_store(section, mode="merge")

    assert any("ghost-ssh" in w for w in result.warnings)
    assert repo.snapshot().root_connection_ids == ()


# ---------------------------------------------------------------------------
# Finding 1: protocol-specific non-SSH launch configuration must round-trip
# ---------------------------------------------------------------------------


def _serial_data(nickname="serial-dev"):
    return {
        "nickname": nickname,
        "protocol": "serial",
        "device": "/dev/ttyUSB0",
        "baud": 115200,
        "flow": "hard",
        "databits": 8,
        "parity": "none",
        "stopbits": 1,
    }


def test_snapshot_for_backup_and_restore_preserve_serial_protocol_fields(tmp_path):
    source, _, _ = _repo(tmp_path / "source")
    source.create_connection(_serial_data())
    section = source.snapshot_for_backup()

    target, _, _ = _repo(tmp_path / "target")
    target.restore_connection_store(section, mode="merge")

    record = target.get_record("serial-dev")
    assert record is not None
    assert record.data.get("device") == "/dev/ttyUSB0"
    assert record.data.get("baud") == 115200
    assert record.data.get("flow") == "hard"
    assert record.data.get("databits") == 8
    assert record.data.get("parity") == "none"
    assert record.data.get("stopbits") == 1


def test_snapshot_for_backup_excludes_password_like_fields_from_plugin_data(tmp_path):
    repo, _, _ = _repo(tmp_path)
    payload = _serial_data("serial-secret")
    payload["password"] = "hunter2"
    payload["api_token"] = "abc123"
    repo.create_connection(payload)

    section = repo.snapshot_for_backup()

    entry = next(c for c in section["connections"] if c["id"] == "serial-secret")
    plugin_data = entry.get("plugin_data") or {}
    assert "password" not in plugin_data
    assert "api_token" not in plugin_data
    assert "password" not in str(section)
    assert "hunter2" not in str(section)
    assert "abc123" not in str(section)


# ---------------------------------------------------------------------------
# Finding 2: group ordering and replace-mode membership semantics
# ---------------------------------------------------------------------------


def test_restore_connection_store_preserves_group_order_with_multiple_siblings(tmp_path):
    repo, _, _ = _repo(tmp_path)
    repo.create_group("Alpha")
    repo.create_group("Beta")
    # Target's current sibling order is Alpha(0), Beta(1) — the backup below
    # declares the opposite relative order.
    section = {
        "version": 1,
        "connections": [],
        "groups": [
            {"id": "beta-src", "name": "Beta", "parent_id": None,
             "order": 0, "color": "", "connection_ids": []},
            {"id": "alpha-src", "name": "Alpha", "parent_id": None,
             "order": 1, "color": "", "connection_ids": []},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    assert [g.name for g in snap.groups] == ["Beta", "Alpha"]


def test_restore_connection_store_replace_mode_matches_backup_membership_exactly(tmp_path):
    """Reproduces the reported bug: a connection moved from one persisting
    group to another in the backup must end up in ONLY the new group after
    a replace-mode restore — not in both."""
    repo, _, _ = _repo(tmp_path)
    repo.create_connection(_telnet_data("server-a"))
    repo.create_connection(_telnet_data("server-b"))
    prod = repo.create_group("Prod")
    qa = repo.create_group("QA")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("server-a",), target_group_id=prod.id)
    )
    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("server-b",), target_group_id=qa.id)
    )

    # Backup: Prod now has server-b, QA now has server-a (swapped).
    section = {
        "version": 1,
        "connections": [
            {"id": "server-a", "protocol": "telnet", "nickname": "server-a",
             "hostname": "10.0.0.5", "username": "", "port": 2323, "display_name": ""},
            {"id": "server-b", "protocol": "telnet", "nickname": "server-b",
             "hostname": "10.0.0.5", "username": "", "port": 2323, "display_name": ""},
        ],
        "groups": [
            {"id": "prod-src", "name": "Prod", "parent_id": None,
             "order": 0, "color": "", "connection_ids": ["server-b"]},
            {"id": "qa-src", "name": "QA", "parent_id": None,
             "order": 1, "color": "", "connection_ids": ["server-a"]},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="replace")

    snap = repo.snapshot()
    memberships = {g.name: set(g.connection_ids) for g in snap.groups}
    assert memberships == {"Prod": {"server-b"}, "QA": {"server-a"}}


def test_restore_connection_store_merge_mode_stays_additive_not_exclusive(tmp_path):
    """Merge mode must remain non-destructive: a connection's pre-existing
    group membership must survive even when the backup places it in a
    different group too."""
    repo, _, _ = _repo(tmp_path)
    repo.create_connection(_telnet_data("shared"))
    prod = repo.create_group("Prod")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("shared",), target_group_id=prod.id)
    )
    section = {
        "version": 1,
        "connections": [
            {"id": "shared", "protocol": "telnet", "nickname": "shared",
             "hostname": "10.0.0.5", "username": "", "port": 2323, "display_name": ""},
        ],
        "groups": [
            {"id": "qa-src", "name": "QA", "parent_id": None,
             "order": 0, "color": "", "connection_ids": ["shared"]},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    memberships = {g.name for g in snap.groups if "shared" in g.connection_ids}
    assert memberships == {"Prod", "QA"}


def test_restore_connection_store_merge_mode_root_placement_preserves_group_membership(tmp_path):
    """Reproduces the reported bug: restoring a backup that places a
    connection at root must not evict it from a group it already belongs to
    on the target under merge mode — root placement must be additive, not
    exclusive."""
    repo, _, _ = _repo(tmp_path)
    repo.create_connection(_telnet_data("shared"))
    prod = repo.create_group("Prod")
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    repo.move_connections(
        MoveConnectionsRequest(connection_ids=("shared",), target_group_id=prod.id)
    )
    section = {
        "version": 1,
        "connections": [
            {"id": "shared", "protocol": "telnet", "nickname": "shared",
             "hostname": "10.0.0.5", "username": "", "port": 2323, "display_name": ""},
        ],
        "groups": [],
        "root_connection_ids": ["shared"],
        "metadata": [],
    }

    repo.restore_connection_store(section, mode="merge")

    snap = repo.snapshot()
    memberships = {g.name for g in snap.groups if "shared" in g.connection_ids}
    assert memberships == {"Prod"}, (
        "merge-mode root placement must not remove existing group membership"
    )
    # A connection that stays grouped cannot also sit in the root list — the
    # backup's root placement for it is a no-op under merge, not a promotion.
    assert "shared" not in snap.root_connection_ids


def test_restore_connection_store_root_placement_merge_vs_replace_semantics(tmp_path):
    """Same initial target state, same backup root placement: merge must
    preserve pre-existing group membership, replace must remove it."""
    section = {
        "version": 1,
        "connections": [
            {"id": "shared", "protocol": "telnet", "nickname": "shared",
             "hostname": "10.0.0.5", "username": "", "port": 2323, "display_name": ""},
        ],
        "groups": [],
        "root_connection_ids": ["shared"],
        "metadata": [],
    }

    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    merge_repo, _, _ = _repo(tmp_path / "merge")
    merge_repo.create_connection(_telnet_data("shared"))
    merge_prod = merge_repo.create_group("Prod")
    merge_repo.move_connections(
        MoveConnectionsRequest(connection_ids=("shared",), target_group_id=merge_prod.id)
    )
    merge_repo.restore_connection_store(section, mode="merge")
    merge_snap = merge_repo.snapshot()
    merge_memberships = {g.name for g in merge_snap.groups if "shared" in g.connection_ids}
    assert merge_memberships == {"Prod"}

    replace_repo, _, _ = _repo(tmp_path / "replace")
    replace_repo.create_connection(_telnet_data("shared"))
    replace_prod = replace_repo.create_group("Prod")
    replace_repo.move_connections(
        MoveConnectionsRequest(connection_ids=("shared",), target_group_id=replace_prod.id)
    )
    replace_repo.restore_connection_store(section, mode="replace")
    replace_snap = replace_repo.snapshot()
    replace_memberships = {g.name for g in replace_snap.groups if "shared" in g.connection_ids}
    assert replace_memberships == set()
    assert replace_snap.root_connection_ids == ("shared",)


@pytest.mark.parametrize(
    "protocol,fields",
    [
        (
            "docker",
            {"container": "web-1", "command": "/bin/bash", "runtime": "docker",
             "docker_host": "unix:///var/run/docker.sock", "user": "root",
             "workdir": "/app"},
        ),
        (
            "k8s",
            {"pod": "web-0", "container": "app", "namespace": "prod",
             "kube_context": "prod-cluster", "kubeconfig": "/home/u/.kube/config",
             "command": "/bin/sh"},
        ),
        (
            "mosh",
            {"keyfile": "/home/u/.ssh/id_ed25519", "extra_ssh_opts": "-o Foo=bar",
             "predict": "adaptive", "mosh_port": 60001},
        ),
    ],
)
def test_snapshot_for_backup_and_restore_preserve_protocol_fields(tmp_path, protocol, fields):
    source, _, _ = _repo(tmp_path / "source")
    payload = {"nickname": "plugin-conn", "protocol": protocol, "hostname": "10.0.0.9"}
    payload.update(fields)
    source.create_connection(payload)
    section = source.snapshot_for_backup()

    target, _, _ = _repo(tmp_path / "target")
    target.restore_connection_store(section, mode="merge")

    record = target.get_record("plugin-conn")
    assert record is not None
    for key, value in fields.items():
        assert record.data.get(key) == value
