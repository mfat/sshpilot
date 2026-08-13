"""Tests for the headless connection repository (daemon-owned store)."""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepository,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.errors import CoreError  # noqa: E402


def _repo(tmp_path, ssh_text: str = "", *, isolated: bool = False):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    state = tmp_path / "connections.json"
    legacy = tmp_path / "config.json"
    return ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=isolated),
        state_path=state,
        legacy_config_path=legacy,
        isolated=isolated,
    ), root, state, legacy


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Initial load
# ---------------------------------------------------------------------------


def test_initial_default_load_is_empty(tmp_path):
    repo, root, state, _ = _repo(tmp_path)
    snap = repo.snapshot()
    assert snap.generation == 0
    assert snap.connections == ()
    assert snap.groups == ()
    assert snap.root_connection_ids == ()


def test_legacy_migration_reconciles_deleted_connections_and_preserves_order(tmp_path):
    repo, root, state, legacy = _repo(
        tmp_path,
        "Host web\n    HostName web.example\n\n"
        "Host db\n    HostName db.example\n",
    )
    del repo
    state.unlink()
    legacy.write_text(
        json.dumps(
            {
                "connections": {"non_ssh": [{"nickname": "serial", "protocol": "serial"}]},
                "connection_groups": {
                    "groups": {
                        "prod": {
                            "id": "prod",
                            "name": "Production",
                            "order": 0,
                            "connections": ["deleted", "db", "db"],
                            "parent_id": "missing",
                        }
                    },
                    "connections": {"deleted": "prod", "web": "prod"},
                    "root_connections": ["deleted", "web", "web"],
                },
                "connections_meta": {
                    "deleted": {"pinned": True},
                    "db": {"tags": ["database"]},
                },
            }
        ),
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=legacy,
        isolated=False,
    )
    snapshot = repo.snapshot()
    assert [item.id for item in snapshot.connections] == ["web", "db", "serial"]
    assert snapshot.groups[0].parent_id is None
    assert snapshot.groups[0].connection_ids == ("db", "web")
    assert snapshot.root_connection_ids == ("serial",)
    assert [item.connection_id for item in snapshot.metadata] == ["db"]
    assert state.exists()
    assert legacy.read_bytes() == before
    assert repo._legacy_migration_result.dangling_memberships_removed == 1
    assert repo._legacy_migration_result.dangling_root_entries_removed == 1
    assert repo._legacy_migration_result.dangling_metadata_entries_removed == 1
    assert repo._legacy_migration_result.dangling_parent_links_removed == 1


def test_legacy_migration_failure_does_not_create_canonical_state(tmp_path):
    repo, root, state, legacy = _repo(
        tmp_path, "Host web\n    HostName web.example\n"
    )
    del repo
    state.unlink()
    legacy.write_text(
        json.dumps(
            {
                "connections_meta": {"deleted": {"password": "secret"}},
            }
        ),
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    with pytest.raises(CoreError):
        ConnectionRepository(
            ssh_store=SshConfigStore(root),
            state_path=state,
            legacy_config_path=legacy,
            isolated=False,
        )
    assert not state.exists()
    assert legacy.read_bytes() == before


def test_canonical_orphan_metadata_is_dormant_and_preserved(tmp_path):
    ssh_text = "Host web\n    HostName web.example\n"
    _, root, state, _ = _repo(tmp_path, ssh_text)
    state.unlink()
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {"groups": {}, "root_connections": ["web"]},
            "metadata": {"deleted": {"pinned": True}},
        },
    )
    before = state.read_bytes()

    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )

    assert repo.snapshot().metadata == ()
    assert state.read_bytes() == before


def test_canonical_orphan_group_and_root_ids_are_filtered_and_preserved(tmp_path):
    root = tmp_path / "ssh_config"
    root.write_text(
        "Host A\n    HostName a.example\n\n"
        "Host B\n    HostName b.example\n"
    )
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {
                "groups": {
                    "prod": {
                        "id": "prod",
                        "name": "Production",
                        "connection_ids": ["A", "OLD_HOST"],
                    }
                },
                "root_connections": ["B", "OLD_ROOT"],
            },
            "metadata": {
                "A": {"pinned": True},
                "OLD_HOST": {"tags": ["dormant"]},
            },
        },
    )
    before = state.read_bytes()

    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )

    snapshot = repo.snapshot()
    assert snapshot.groups[0].connection_ids == ("A",)
    assert snapshot.root_connection_ids == ("B",)
    assert [item.connection_id for item in snapshot.metadata] == ["A"]
    assert state.read_bytes() == before

    root.write_text(
        "Host A\n    HostName a.example\n\n"
        "Host B\n    HostName b.example\n\n"
        "Host OLD_HOST\n    HostName old.example\n\n"
        "Host OLD_ROOT\n    HostName root.example\n"
    )
    snapshot = repo.reload()
    assert snapshot.groups[0].connection_ids == ("A", "OLD_HOST")
    assert snapshot.root_connection_ids == ("B", "OLD_ROOT")
    assert [item.connection_id for item in snapshot.metadata] == [
        "A",
        "OLD_HOST",
    ]
    assert state.read_bytes() == before


def test_temporary_include_disappearance_preserves_dormant_decorations(tmp_path):
    root = tmp_path / "ssh_config"
    included = tmp_path / "fragment.conf"
    root.write_text(
        "Include fragment.conf\n\n"
        "Host Root\n    HostName root.example\n"
    )
    included.write_text("Host Included\n    HostName included.example\n")
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {
                "groups": {
                    "prod": {
                        "id": "prod",
                        "name": "Production",
                        "connection_ids": ["Included"],
                    }
                },
                "root_connections": ["Root"],
            },
            "metadata": {"Included": {"pinned": True}},
        },
    )
    before = state.read_bytes()
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    assert [record.id for record in repo.snapshot().connections] == [
        "Root",
        "Included",
    ]

    included.unlink()
    snapshot = repo.reload()
    assert [record.id for record in snapshot.connections] == ["Root"]
    assert snapshot.groups[0].connection_ids == ()
    assert snapshot.metadata == ()
    assert snapshot.root_connection_ids == ("Root",)
    assert state.read_bytes() == before

    included.write_text("Host Included\n    HostName included.example\n")
    snapshot = repo.reload()
    assert [record.id for record in snapshot.connections] == [
        "Root",
        "Included",
    ]
    assert snapshot.groups[0].connection_ids == ("Included",)
    assert snapshot.metadata[0].connection_id == "Included"
    assert state.read_bytes() == before


def test_legacy_parent_cycle_aborts_without_canonical_file(tmp_path):
    _, root, state, legacy = _repo(tmp_path, "Host web\n    HostName web.example\n")
    state.unlink()
    legacy.write_text(
        json.dumps(
            {
                "connection_groups": {
                    "groups": {
                        "a": {"name": "A", "parent_id": "b", "connections": []},
                        "b": {"name": "B", "parent_id": "a", "connections": []},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ConnectionRepository(
            ssh_store=SshConfigStore(root),
            state_path=state,
            legacy_config_path=legacy,
            isolated=False,
        )
    assert not state.exists()


def test_loads_ssh_connections_in_order(tmp_path):
    repo, root, state, _ = _repo(
        tmp_path,
        "Host web\n    HostName example.com\n    User alice\n\n"
        "Host db\n    HostName db.internal\n",
    )
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["web", "db"]
    assert [c.nickname for c in snap.connections] == ["web", "db"]
    assert snap.connections[0].hostname == "example.com"
    assert snap.root_connection_ids == ("web", "db")
    assert repo.get_record("web") is not None
    assert repo.get_record("nope") is None


def test_isolated_load_marks_records(tmp_path):
    repo, root, state, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n", isolated=True
    )
    record = repo.get_record("web")
    assert record is not None
    assert record.data.get("isolated_mode") is True


def test_loads_non_ssh_records_from_state_file(tmp_path):
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [
                {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5", "port": 2323}
            ],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    repo, root, _, _ = _repo(tmp_path)
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["tel"]
    assert snap.connections[0].protocol == "telnet"


def test_loads_groups_and_metadata_from_state_file(tmp_path):
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {
                "groups": {
                    "prod": {
                        "id": "prod",
                        "name": "Production",
                        "order": 0,
                        "color": "#ff0000",
                        "connection_ids": ["web"],
                    }
                },
                "root_connections": [],
            },
            "metadata": {"web": {"pinned": True, "tags": ["prod"]}},
        },
    )
    repo, root, _, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n    User alice\n"
    )
    snap = repo.snapshot()
    assert [g.id for g in snap.groups] == ["prod"]
    group = snap.groups[0]
    assert group.name == "Production"
    assert group.color == "#ff0000"
    assert group.connection_ids == ("web",)
    assert snap.root_connection_ids == ()
    assert [m.connection_id for m in snap.metadata] == ["web"]
    assert snap.metadata[0].values["pinned"] is True
    assert snap.connections[0].groups[0].id == "prod"


def test_migrates_legacy_config_json(tmp_path):
    legacy = tmp_path / "config.json"
    legacy.write_text(
        json.dumps(
            {
                "connections": {
                    "non_ssh": [
                        {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
                    ]
                },
                "connection_groups": {
                    "groups": {
                        "prod": {"id": "prod", "name": "Production", "connections": ["web"]}
                    },
                    "connections": {"web": "prod"},
                    "root_connections": [],
                },
                "connections_meta": {"web": {"pinned": True}},
            }
        )
    )
    repo, root, state, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n    User alice\n"
    )
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ["web", "tel"]
    assert [g.id for g in snap.groups] == ["prod"]
    assert snap.metadata[0].values["pinned"] is True
    # The dedicated file was written; legacy values were not deleted.
    assert state.exists()
    original = json.loads(legacy.read_text())
    assert "web" in original["connections_meta"]


def test_no_partial_state_when_ssh_config_unreadable(tmp_path):
    root = tmp_path / "ssh_config"
    root.write_text("Host web\n    HostName example.com\n")
    repo, root, state, legacy = _repo(tmp_path, "Host web\n    HostName example.com\n")
    before = repo.snapshot()
    # A subsequent reload with an unreadable tree must leave state untouched:
    # replace the config with a directory so reading it fails.
    root.unlink()
    root.mkdir()
    with pytest.raises(CoreError):
        repo.reload()
    assert repo.snapshot().connections == before.connections
    assert repo.snapshot().generation == before.generation


def test_malformed_canonical_state_does_not_block_ssh_startup(tmp_path, caplog):
    root = tmp_path / "ssh_config"
    root.write_text("Host web\n    HostName web.example\n")
    state = tmp_path / "connections.json"
    state.write_bytes(b"{not-json")
    before = state.read_bytes()
    with caplog.at_level(logging.WARNING):
        repo, _root, _state, _legacy = _repo(tmp_path)
    assert [record.id for record in repo.snapshot().connections] == ["web"]
    assert state.read_bytes() == before
    assert "Failed to load auxiliary connection state" in caplog.text


def test_unsupported_canonical_state_does_not_block_ssh_startup(tmp_path):
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 99,
            "non_ssh_connections": [],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    before = state.read_bytes()
    repo, _root, _state, _legacy = _repo(
        tmp_path, "Host web\n    HostName web.example\n"
    )
    assert [record.id for record in repo.snapshot().connections] == ["web"]
    assert state.read_bytes() == before


@pytest.mark.parametrize(
    "groups",
    [
        {
            "g1": {
                "id": "g1",
                "name": "G1",
                "parent_id": "missing",
            }
        },
        {
            "g1": {
                "id": "g1",
                "name": "G1",
                "parent_id": "g2",
            },
            "g2": {
                "id": "g2",
                "name": "G2",
                "parent_id": "g1",
            },
        },
    ],
    ids=["unknown-parent", "parent-cycle"],
)
def test_inconsistent_canonical_groups_do_not_block_ssh_startup(
    tmp_path, groups
):
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {"groups": groups, "root_connections": []},
            "metadata": {},
        },
    )
    before = state.read_bytes()
    repo, _root, _state, _legacy = _repo(
        tmp_path, "Host web\n    HostName web.example\n"
    )
    assert [record.id for record in repo.snapshot().connections] == ["web"]
    assert repo.snapshot().groups == ()
    assert state.read_bytes() == before


def test_dormant_root_order_survives_unrelated_sidecar_writes(tmp_path):
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {
                "groups": {},
                "root_connections": ["A", "MISSING", "B"],
            },
            "metadata": {},
        },
    )
    repo, root, _state, _legacy = _repo(
        tmp_path,
        "Host A\n    HostName a.example\n\n"
        "Host B\n    HostName b.example\n",
    )
    assert repo.snapshot().root_connection_ids == ("A", "B")

    repo.update_connection_metadata("A", {"pinned": True})
    assert json.loads(state.read_text())["groups"]["root_connections"] == [
        "A",
        "MISSING",
        "B",
    ]

    group = repo.create_group("Production")
    repo.rename_group(group.id, "Production Servers")
    repo.set_group_color(group.id, "#ff0000")
    assert json.loads(state.read_text())["groups"]["root_connections"] == [
        "A",
        "MISSING",
        "B",
    ]

    root.write_text(
        "Host A\n    HostName a.example\n\n"
        "Host MISSING\n    HostName missing.example\n\n"
        "Host B\n    HostName b.example\n"
    )
    assert repo.reload().root_connection_ids == ("A", "MISSING", "B")


def test_external_root_removal_stays_dormant_but_managed_delete_removes_it(
    tmp_path,
):
    repo, root, state, _legacy = _repo(
        tmp_path,
        "Host A\n    HostName a.example\n\n"
        "Host B\n    HostName b.example\n",
    )

    root.write_text("Host B\n    HostName b.example\n")
    assert repo.reload().root_connection_ids == ("B",)
    assert json.loads(state.read_text())["groups"]["root_connections"] == [
        "A",
        "B",
    ]

    root.write_text(
        "Host A\n    HostName a.example\n\n"
        "Host B\n    HostName b.example\n",
    )
    repo.reload()
    repo.delete_connection("A")
    assert json.loads(state.read_text())["groups"]["root_connections"] == ["B"]


# ---------------------------------------------------------------------------
# Snapshot validation
# ---------------------------------------------------------------------------


def test_snapshot_filters_unknown_group_membership(tmp_path):
    state = tmp_path / "connections.json"
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [],
            "groups": {
                "groups": {
                    "g1": {"id": "g1", "name": "G1", "connection_ids": ["ghost"]}
                },
                "root_connections": [],
            },
            "metadata": {},
        },
    )
    repo, _root, state, _ = _repo(tmp_path)
    assert repo.snapshot().groups[0].connection_ids == ()
    assert json.loads(state.read_text())["groups"]["groups"]["g1"][
        "connection_ids"
    ] == ["ghost"]


# ---------------------------------------------------------------------------
# Reload and generation
# ---------------------------------------------------------------------------


def test_stable_noop_reload_does_not_increment_generation(tmp_path):
    repo, root, state, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n"
    )
    assert repo.snapshot().generation == 0
    after = repo.reload()
    assert after.generation == 0


def test_reload_after_external_change_increments_generation(tmp_path):
    repo, root, state, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n"
    )
    root.write_text(
        "Host web\n    HostName example.com\n\n"
        "Host other\n    HostName o.example.com\n"
    )
    events = []
    repo.add_listener(lambda change: events.append(change))
    after = repo.reload()
    assert after.generation == 1
    assert [c.id for c in after.connections] == ["web", "other"]
    assert len(events) == 1
    assert events[0].before.generation == 0
    assert events[0].after.generation == 1


def test_reload_after_state_file_change(tmp_path):
    repo, root, state, _ = _repo(tmp_path)
    _write_state(
        state,
        {
            "version": 1,
            "non_ssh_connections": [
                {"nickname": "tel", "protocol": "telnet", "hostname": "10.0.0.5"}
            ],
            "groups": {"groups": {}, "root_connections": []},
            "metadata": {},
        },
    )
    after = repo.reload()
    assert after.generation == 1
    assert [c.id for c in after.connections] == ["tel"]


# ---------------------------------------------------------------------------
# Listeners and threads
# ---------------------------------------------------------------------------


def test_listener_reentrancy(tmp_path):
    repo, root, state, _ = _repo(tmp_path)
    seen = []

    def callback(change):
        seen.append(change.after.generation)
        # Re-entrant snapshot from inside the callback must not deadlock.
        assert repo.snapshot().generation == change.after.generation

    repo.add_listener(callback)
    root.write_text("Host a\n    HostName a.example.com\n")
    repo.reload()
    assert seen == [1]


def test_listener_runs_after_repository_lock_release(tmp_path):
    repo, root, _state, _ = _repo(tmp_path)
    completed = threading.Event()

    def callback(change):
        assert repo.snapshot().generation == change.after.generation
        reader = threading.Thread(target=lambda: (repo.snapshot(), completed.set()))
        reader.start()
        reader.join(1)
        assert completed.is_set()

    repo.add_listener(callback)
    root.write_text("Host a\n    HostName a.example.com\n")
    repo.reload()


def test_thread_serialization(tmp_path):
    repo, root, state, _ = _repo(tmp_path)
    errors = []
    barrier = threading.Barrier(3)

    def reader():
        barrier.wait()
        try:
            for _ in range(20):
                repo.snapshot()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def writer():
        barrier.wait()
        try:
            for i in range(20):
                root.write_text(f"Host h{i}\n    HostName h{i}.example.com\n")
                repo.reload()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()
    assert not errors


def test_discover_paths(tmp_path):
    root = tmp_path / "ssh_config"
    root.write_text("Host web\n    HostName example.com\n")
    state = tmp_path / "connections.json"
    repo, root, state, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n"
    )
    paths = repo.discover_paths()
    assert state in paths
    assert any(str(p) == str(root) for p in paths)


# ---------------------------------------------------------------------------
# Editor records
# ---------------------------------------------------------------------------


def test_editor_record_carries_generation(tmp_path):
    repo, root, state, _ = _repo(
        tmp_path, "Host web\n    HostName example.com\n"
    )
    editor = repo.get_editor_record("web")
    assert editor is not None
    assert editor.generation == 0
    assert editor.source  # daemon-recorded source metadata
