"""Group-operation tests for the headless connection repository."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepository,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402
from sshpilot.api.models.common import ConnectionId  # noqa: E402
from sshpilot.api.models.connection_store import MoveConnectionsRequest  # noqa: E402


def _repo(tmp_path, ssh_text: str = ""):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    ), root, tmp_path / "connections.json"


def _state(path: Path) -> dict:
    return json.loads(path.read_text())


def _seed_web(repo):
    return repo.create_connection(
        {"nickname": "web", "hostname": "example.com", "protocol": "ssh"}
    )


# ---------------------------------------------------------------------------
# Group lifecycle
# ---------------------------------------------------------------------------


def test_create_group_and_persist(tmp_path):
    repo, root, state = _repo(tmp_path)
    group = repo.create_group("Production", color="#ff0000")
    assert group.name == "Production"
    assert group.color == "#ff0000"
    stored = _state(state)
    assert stored["groups"]["groups"][group.id]["name"] == "Production"
    snap = repo.snapshot()
    assert snap.groups[0].id == group.id


def test_create_nested_group(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Production")
    child = repo.create_group("Web", parent_id=parent.id)
    snap = repo.snapshot()
    child_summary = next(g for g in snap.groups if g.id == child.id)
    assert child_summary.parent_id == parent.id


def test_rename_group(tmp_path):
    repo, root, state = _repo(tmp_path)
    group = repo.create_group("Old")
    repo.rename_group(group.id, "New")
    snap = repo.snapshot()
    renamed = next(g for g in snap.groups if g.id == group.id)
    assert renamed.name == "New"
    assert _state(state)["groups"]["groups"][group.id]["name"] == "New"


def test_set_group_color(tmp_path):
    repo, root, state = _repo(tmp_path)
    group = repo.create_group("G1")
    repo.set_group_color(group.id, "#00ff00")
    snap = repo.snapshot()
    assert next(g for g in snap.groups if g.id == group.id).color == "#00ff00"


def test_place_group_orders_siblings(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.place_group(g1.id, parent.id, 0)
    repo.place_group(g2.id, parent.id, 0)
    snap = repo.snapshot()
    g1s = next(g for g in snap.groups if g.id == g1.id)
    g2s = next(g for g in snap.groups if g.id == g2.id)
    assert g1s.parent_id == parent.id and g2s.parent_id == parent.id
    assert (g2s.order, g2s.id) < (g1s.order, g1s.id)


def test_place_group_reorder_with_current_generation_succeeds(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    g1 = repo.create_group("G1", parent_id=parent.id)
    g2 = repo.create_group("G2", parent_id=parent.id)
    before = repo.snapshot()
    repo.place_group(g2.id, parent.id, 0, expected_generation=before.generation)
    snap = repo.snapshot()
    g1s = next(g for g in snap.groups if g.id == g1.id)
    g2s = next(g for g in snap.groups if g.id == g2.id)
    assert g1s.parent_id == parent.id and g2s.parent_id == parent.id
    assert (g2s.order, g2s.id) < (g1s.order, g1s.id)
    assert snap.generation == before.generation + 1


def test_place_group_reparent_with_current_generation_succeeds(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    child = repo.create_group("Child")
    before = repo.snapshot()
    repo.place_group(child.id, parent.id, 0, expected_generation=before.generation)
    snap = repo.snapshot()
    placed = next(g for g in snap.groups if g.id == child.id)
    assert placed.parent_id == parent.id


def test_stale_place_group_reorder_does_not_mutate(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    g1 = repo.create_group("G1", parent_id=parent.id)
    g2 = repo.create_group("G2", parent_id=parent.id)
    before = repo.snapshot()
    with pytest.raises(CoreError) as exc:
        repo.place_group(
            g2.id, parent.id, 0, expected_generation=before.generation - 1
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert repo.snapshot() == before
    # Sibling order is provably unchanged.
    after = repo.snapshot()
    g1s = next(g for g in after.groups if g.id == g1.id)
    g2s = next(g for g in after.groups if g.id == g2.id)
    assert g1s.parent_id == parent.id and g2s.parent_id == parent.id
    assert (g1s.order, g1s.id) < (g2s.order, g2s.id)


def test_stale_place_group_reparent_does_not_mutate(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    child = repo.create_group("Child")
    before = repo.snapshot()
    with pytest.raises(CoreError) as exc:
        repo.place_group(
            child.id, parent.id, 0, expected_generation=before.generation - 1
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert repo.snapshot() == before


def test_place_group_rejects_cycle(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    child = repo.create_group("Child", parent_id=parent.id)
    with pytest.raises(CoreError) as exc:
        repo.place_group(parent.id, child.id, 0)
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


def test_delete_group_moves_children_to_parent(tmp_path):
    repo, root, state = _repo(tmp_path)
    parent = repo.create_group("Parent")
    child = repo.create_group("Child", parent_id=parent.id)
    repo.delete_group(parent.id)
    snap = repo.snapshot()
    assert parent.id not in [g.id for g in snap.groups]
    child_summary = next(g for g in snap.groups if g.id == child.id)
    assert child_summary.parent_id is None


def test_delete_group_preserves_other_memberships(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("web", g1.id)
    repo.copy_connection_to_group("web", g2.id)
    repo.delete_group(g1.id)
    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert [ref.id for ref in web.groups] == [g2.id]


def test_delete_group_moves_last_membership_to_root(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    repo.copy_connection_to_group("web", g1.id)
    assert "web" not in repo.snapshot().root_connection_ids
    repo.delete_group(g1.id)
    snap = repo.snapshot()
    assert "web" in snap.root_connection_ids
    assert _state(state)["groups"]["root_connections"] == ["web"]


# ---------------------------------------------------------------------------
# Membership operations
# ---------------------------------------------------------------------------


def test_assign_connection_to_group_moves(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("web", g1.id)
    repo.assign_connection_to_group("web", g2.id)
    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert [ref.id for ref in web.groups] == [g2.id]
    assert _state(state)["groups"]["groups"][g2.id]["connection_ids"] == ["web"]


def test_assign_connection_to_root(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    repo.assign_connection_to_group("web", g1.id)
    repo.assign_connection_to_group("web", None)
    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert web.groups == ()
    assert "web" in snap.root_connection_ids


def test_copy_preserves_existing_memberships(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("web", g1.id)
    repo.copy_connection_to_group("web", g2.id)
    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert {ref.id for ref in web.groups} == {g1.id, g2.id}


def test_remove_one_membership_only(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("web", g1.id)
    repo.copy_connection_to_group("web", g2.id)
    repo.remove_connection_from_group("web", g1.id)
    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert [ref.id for ref in web.groups] == [g2.id]


def test_reorder_connection_within_group(tmp_path):
    repo, root, state = _repo(tmp_path)
    for name in ("A", "B", "C"):
        repo.create_connection(
            {"nickname": name, "hostname": f"{name}.test", "protocol": "ssh"}
        )
    g1 = repo.create_group("G1")
    for name in ("A", "B", "C"):
        repo.copy_connection_to_group(name, g1.id)
    repo.reorder_connection("C", "A", g1.id, "above")
    snap = repo.snapshot()
    group = next(g for g in snap.groups if g.id == g1.id)
    assert group.connection_ids.index("C") < group.connection_ids.index("A")


def test_reorder_connection_in_root(tmp_path):
    repo, root, state = _repo(tmp_path)
    for name in ("A", "B", "C"):
        repo.create_connection(
            {"nickname": name, "hostname": f"{name}.test", "protocol": "ssh"}
        )
    repo.reorder_connection("C", "A", None, "above")
    snap = repo.snapshot()
    assert snap.root_connection_ids.index("C") < snap.root_connection_ids.index("A")


# ---------------------------------------------------------------------------
# Events and persistence
# ---------------------------------------------------------------------------


def test_each_group_operation_emits_one_change(tmp_path):
    repo, root, state = _repo(tmp_path)
    events = []
    repo.add_listener(lambda change: events.append(change))
    group = repo.create_group("G1")
    repo.rename_group(group.id, "G2")
    repo.set_group_color(group.id, "#000000")
    repo.delete_group(group.id)
    assert len(events) == 4
    gens = [e.after.generation for e in events]
    assert gens == [1, 2, 3, 4]


def test_group_expansion_not_stored(tmp_path):
    repo, root, state = _repo(tmp_path)
    group = repo.create_group("G1")
    stored = _state(state)
    assert "collapsed" not in stored["groups"]["groups"][group.id]
    assert "expanded" not in stored["groups"]["groups"][group.id]


def test_set_group_color_clears_with_empty_string(tmp_path):
    repo, root, state = _repo(tmp_path)
    group = repo.create_group("G1", color="#ff0000")
    repo.set_group_color(group.id, "")
    snap = repo.snapshot()
    assert next(g for g in snap.groups if g.id == group.id).color == ""


# ---------------------------------------------------------------------------
# Multi-membership edge cases (migrated from the retired GroupManager tests)
# ---------------------------------------------------------------------------


def test_copy_connection_is_idempotent(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    repo.copy_connection_to_group("web", g1.id)
    repo.copy_connection_to_group("web", g1.id)
    snap = repo.snapshot()
    group = next(g for g in snap.groups if g.id == g1.id)
    assert group.connection_ids.count("web") == 1


def test_copy_ungrouped_connection_leaves_root(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    assert "web" in repo.snapshot().root_connection_ids
    g1 = repo.create_group("G1")
    repo.copy_connection_to_group("web", g1.id)
    snap = repo.snapshot()
    assert "web" not in snap.root_connection_ids
    assert snap.root_connection_ids == ()


def test_remove_last_membership_returns_to_root(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    repo.copy_connection_to_group("web", g1.id)
    repo.remove_connection_from_group("web", g1.id)
    snap = repo.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert web.groups == ()
    assert "web" in snap.root_connection_ids


def test_stale_move_connections_does_not_mutate(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection({"nickname": "a", "hostname": "a.example", "protocol": "ssh"})
    repo.create_connection({"nickname": "b", "hostname": "b.example", "protocol": "ssh"})
    before = repo.snapshot()
    with pytest.raises(CoreError) as exc:
        repo.move_connections(
            MoveConnectionsRequest(
                connection_ids=(ConnectionId("a"),),
                target_connection_id=ConnectionId("b"),
                position="above",
                expected_generation=before.generation - 1,
            )
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert repo.snapshot() == before


def test_multi_membership_survives_fresh_load(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("web", g1.id)
    repo.copy_connection_to_group("web", g2.id)
    repo2 = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    snap = repo2.snapshot()
    web = next(c for c in snap.connections if c.id == "web")
    assert {ref.id for ref in web.groups} == {g1.id, g2.id}
    assert "web" not in snap.root_connection_ids


def test_rename_preserves_multiple_memberships_across_reload(tmp_path):
    repo, root, state = _repo(tmp_path, "Host web\n    HostName example.com\n")
    g1 = repo.create_group("G1")
    g2 = repo.create_group("G2")
    repo.copy_connection_to_group("web", g1.id)
    repo.copy_connection_to_group("web", g2.id)
    repo.update_connection(
        "web",
        {"nickname": "web2", "hostname": "example.com", "protocol": "ssh"},
        expected_generation=0,
    )
    snap = repo.snapshot()
    renamed = next(c for c in snap.connections if c.id == "web2")
    assert {ref.id for ref in renamed.groups} == {g1.id, g2.id}
    # No old alias remains in any membership data.
    assert "web" not in [cid for g in snap.groups for cid in g.connection_ids]
    assert "web" not in snap.root_connection_ids
    # Memberships persist across a fresh repository load.
    repo2 = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    snap2 = repo2.snapshot()
    renamed2 = next(c for c in snap2.connections if c.id == "web2")
    assert {ref.id for ref in renamed2.groups} == {g1.id, g2.id}
