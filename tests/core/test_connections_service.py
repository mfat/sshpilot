"""Tests for multi-group membership and group operations in ConnectionService."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections import ConnectionService, MutationKind  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402
from sshpilot.api.models.common import ConnectionId  # noqa: E402
from sshpilot.api.models.connection_store import (  # noqa: E402
    ConnectionPlacementMode,
    MoveConnectionsRequest,
)


@pytest.fixture
def service():
    return ConnectionService(autosave=False)


def _conn(service, name, **extra):
    data = {"nickname": name, "hostname": f"{name}.test", "username": "u"}
    data.update(extra)
    return service.create(data)


def test_move_connections_preserves_block_order_and_exclusive_membership(service):
    group = service.create_group("G1")
    other = service.create_group("G2")
    a = _conn(service, "A")
    b = _conn(service, "B")
    c = _conn(service, "C")
    service.copy_connection_to_group(a.id, other.id)
    service.copy_connection_to_group(b.id, other.id)
    request = types.SimpleNamespace(
        connection_ids=(a.id, b.id),
        target_group_id=group.id,
        target_connection_id=c.id,
        position="above",
        expected_generation=None,
    )
    service.copy_connection_to_group(c.id, group.id)
    service.move_connections(request)
    assert service.list_groups()[0].connection_ids == [a.id, b.id, c.id]
    assert all(a.id not in g.connection_ids for g in service.list_groups() if g.id == other.id)


def test_move_connections_rejects_target_outside_destination(service):
    group = service.create_group("G1")
    a = _conn(service, "A")
    b = _conn(service, "B")
    request = types.SimpleNamespace(
        connection_ids=(a.id,),
        target_group_id=group.id,
        target_connection_id=b.id,
        position="below",
        expected_generation=None,
    )
    with pytest.raises(CoreError):
        service.move_connections(request)


def test_preserving_reorder_retains_secondary_membership(service):
    web = service.create_group("Web")
    prod = service.create_group("Prod")
    a = _conn(service, "A")
    b = _conn(service, "B")
    service.copy_connection_to_group(a.id, prod.id)
    service.copy_connection_to_group(a.id, web.id)
    service.copy_connection_to_group(b.id, web.id)
    service.move_connections(
        MoveConnectionsRequest(
            connection_ids=(ConnectionId(a.id),),
            source_group_id=web.id,
            target_group_id=web.id,
            target_connection_id=ConnectionId(b.id),
            position="below",
            mode=ConnectionPlacementMode.PRESERVE,
        )
    )
    groups = {group.id: group.connection_ids for group in service.list_groups()}
    assert groups[web.id] == [b.id, a.id]
    assert a.id in groups[prod.id]


def test_preserving_root_reorder_does_not_ungroup(service):
    web = service.create_group("Web")
    a = _conn(service, "A")
    b = _conn(service, "B")
    c = _conn(service, "C")
    service.copy_connection_to_group(a.id, web.id)
    service.move_connections(
        MoveConnectionsRequest(
            connection_ids=(ConnectionId(c.id),),
            target_connection_id=ConnectionId(b.id),
            position="above",
            mode=ConnectionPlacementMode.PRESERVE,
        )
    )
    assert a.id in next(group for group in service.list_groups() if group.id == web.id).connection_ids
    assert service.root_order() == [c.id, b.id]


def test_additive_placement_preserves_existing_memberships(service):
    web = service.create_group("Web")
    prod = service.create_group("Prod")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, web.id)
    service.move_connections(
        MoveConnectionsRequest(
            connection_ids=(ConnectionId(a.id),),
            target_group_id=prod.id,
            mode=ConnectionPlacementMode.ADDITIVE,
        )
    )
    groups = {group.id: group.connection_ids for group in service.list_groups()}
    assert a.id in groups[web.id]
    assert a.id in groups[prod.id]


def test_explicit_exclusive_move_removes_secondary_membership(service):
    web = service.create_group("Web")
    prod = service.create_group("Prod")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, web.id)
    service.copy_connection_to_group(a.id, prod.id)
    service.move_connections(
        MoveConnectionsRequest(
            connection_ids=(ConnectionId(a.id),),
            target_group_id=web.id,
            mode=ConnectionPlacementMode.EXCLUSIVE,
        )
    )
    groups = {group.id: group.connection_ids for group in service.list_groups()}
    assert a.id in groups[web.id]
    assert a.id not in groups[prod.id]


# ---------------------------------------------------------------------------
# Membership is derived from group records
# ---------------------------------------------------------------------------


def test_group_id_is_a_projection_of_group_membership(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A")
    assert a.group_id is None
    service.copy_connection_to_group(a.id, g1.id)
    assert service.get(a.id).group_id == g1.id
    service.copy_connection_to_group(a.id, g2.id)
    assert service.get(a.id).group_id == g1.id  # first group by order
    service.remove_connection_from_group(a.id, g1.id)
    assert service.get(a.id).group_id == g2.id
    service.remove_connection_from_group(a.id, g2.id)
    assert service.get(a.id).group_id is None


def test_membership_derived_from_group_records(service):
    g1 = service.create_group("G1")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    g1_record = service.list_groups()[0]
    assert a.id in g1_record.connection_ids


def test_root_order_contains_only_ungrouped(service):
    g1 = service.create_group("G1")
    a = _conn(service, "A")
    b = _conn(service, "B")
    c = _conn(service, "C")
    service.copy_connection_to_group(a.id, g1.id)
    root = service._root_order  # internal, but the contract is observable
    assert set(root) == {b.id, c.id}
    assert a.id not in root


# ---------------------------------------------------------------------------
# Multi-membership operations
# ---------------------------------------------------------------------------


def test_copy_preserves_existing_memberships(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.copy_connection_to_group(a.id, g2.id)
    group_ids = {g.id for g in service.list_groups() if a.id in g.connection_ids}
    assert group_ids == {g1.id, g2.id}
    assert a.id not in service._root_order


def test_remove_one_membership_only(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.copy_connection_to_group(a.id, g2.id)
    service.remove_connection_from_group(a.id, g1.id)
    group_ids = {g.id for g in service.list_groups() if a.id in g.connection_ids}
    assert group_ids == {g2.id}


def test_assign_group_moves_to_one_group(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.assign_group(a.id, g2.id)
    group_ids = {g.id for g in service.list_groups() if a.id in g.connection_ids}
    assert group_ids == {g2.id}
    assert service.get(a.id).group_id == g2.id


def test_remove_from_group_moves_to_root(service):
    g1 = service.create_group("G1")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.remove_from_group(a.id)
    assert service.get(a.id).group_id is None
    assert a.id in service._root_order


def test_create_with_group_ids_multi_membership(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A", group_ids=[g1.id, g2.id])
    group_ids = {g.id for g in service.list_groups() if a.id in g.connection_ids}
    assert group_ids == {g1.id, g2.id}
    assert a.group_id == g1.id


# ---------------------------------------------------------------------------
# Ordering within a group and root
# ---------------------------------------------------------------------------


def test_reorder_connection_within_group(service):
    g1 = service.create_group("G1")
    a, b, c = _conn(service, "A"), _conn(service, "B"), _conn(service, "C")
    for conn in (a, b, c):
        service.copy_connection_to_group(conn.id, g1.id)
    service.reorder_connection(c.id, a.id, g1.id, "above")
    group = next(g for g in service.list_groups() if g.id == g1.id)
    assert group.connection_ids.index(c.id) < group.connection_ids.index(a.id)
    service.reorder_connection(b.id, c.id, g1.id, "below")
    group = next(g for g in service.list_groups() if g.id == g1.id)
    assert group.connection_ids.index(b.id) > group.connection_ids.index(c.id)


def test_reorder_connection_in_root(service):
    a = _conn(service, "A")
    _conn(service, "B")
    c = _conn(service, "C")
    service.reorder_connection(c.id, a.id, None, "above")
    assert service._root_order.index(c.id) < service._root_order.index(a.id)


def test_reorder_connection_invalid_position(service):
    a = _conn(service, "A")
    with pytest.raises(CoreError) as exc:
        service.reorder_connection(a.id, a.id, None, "sideways")
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# Group placement, color, cycles
# ---------------------------------------------------------------------------


def test_place_group_sets_parent_and_sibling_order(service):
    parent = service.create_group("Parent")
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    service.place_group(g1.id, parent.id, 0)
    service.place_group(g2.id, parent.id, 0)
    g1 = next(g for g in service.list_groups() if g.id == g1.id)
    g2 = next(g for g in service.list_groups() if g.id == g2.id)
    assert g1.parent_id == parent.id
    assert g2.parent_id == parent.id
    assert (g2.order, g2.id) < (g1.order, g1.id)  # g2 placed at index 0


def test_place_group_rejects_self_parent(service):
    g1 = service.create_group("G1")
    with pytest.raises(CoreError) as exc:
        service.place_group(g1.id, g1.id, 0)
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


def test_place_group_rejects_cycle(service):
    parent = service.create_group("Parent")
    child = service.create_group("Child", parent_id=parent.id)
    with pytest.raises(CoreError) as exc:
        service.place_group(parent.id, child.id, 0)
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


def test_place_group_rejects_bad_index_type(service):
    g1 = service.create_group("G1")
    with pytest.raises(CoreError):
        service.place_group(g1.id, None, True)  # bool is not an exact int


def test_set_group_color(service):
    g1 = service.create_group("G1")
    service.set_group_color(g1.id, "#ff0000")
    assert next(g for g in service.list_groups() if g.id == g1.id).color == "#ff0000"
    service.set_group_color(g1.id, "")
    assert next(g for g in service.list_groups() if g.id == g1.id).color == ""


def test_delete_group_moves_children_to_parent(service):
    parent = service.create_group("Parent")
    child = service.create_group("Child", parent_id=parent.id)
    service.delete_group(parent.id)
    child = next(g for g in service.list_groups() if g.id == child.id)
    assert child.parent_id is None


def test_delete_group_preserves_other_memberships(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.copy_connection_to_group(a.id, g2.id)
    service.delete_group(g1.id)
    group_ids = {g.id for g in service.list_groups() if a.id in g.connection_ids}
    assert group_ids == {g2.id}
    assert service.get(a.id).group_id == g2.id


def test_delete_group_moves_last_membership_to_root(service):
    g1 = service.create_group("G1")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.delete_group(g1.id)
    assert service.get(a.id).group_id is None
    assert a.id in service._root_order


# ---------------------------------------------------------------------------
# Rename / delete reference migration
# ---------------------------------------------------------------------------


def test_rename_updates_references_in_every_group_and_root(service):
    g1 = service.create_group("G1")
    g2 = service.create_group("G2")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.copy_connection_to_group(a.id, g2.id)
    service.update(a.id, {"nickname": "A2"})
    for g in service.list_groups():
        assert a.id not in g.connection_ids
        assert "A2" in g.connection_ids
    assert service.get("A2") is not None

    b = _conn(service, "B")
    service.update(b.id, {"nickname": "B2"})
    assert "B2" in service._root_order
    assert "B" not in service._root_order


def test_delete_removes_references_everywhere(service):
    g1 = service.create_group("G1")
    a = _conn(service, "A")
    b = _conn(service, "B")
    service.copy_connection_to_group(a.id, g1.id)
    service.delete(a.id)
    assert service.get(a.id) is None
    for g in service.list_groups():
        assert a.id not in g.connection_ids
    assert a.id not in service._root_order
    assert b.id in service._root_order


# ---------------------------------------------------------------------------
# Replace-all transaction
# ---------------------------------------------------------------------------


def test_replace_all_is_transactional_on_bad_groups(service):
    _conn(service, "Keep")
    bad_payload = [
        {"id": "X", "nickname": "X", "hostname": "x.test", "username": "u"},
    ]
    bad_groups = {
        "groups": {
            "g1": {
                "id": "g1",
                "name": "G1",
                "connections": ["X", "missing"],
            }
        },
        "root_connections": ["X"],
    }
    # Unknown memberships are tolerated (filtered); state still replaced.
    service.replace_all(bad_payload, groups=bad_groups)
    assert service.get("X") is not None
    assert service.get("Keep") is None


def test_replace_all_rebuilds_membership(service):
    g1 = service.create_group("G1")
    _conn(service, "A")
    service.replace_all(
        [{"id": "A", "nickname": "A", "hostname": "a.test", "username": "u"}],
        groups={
            "groups": {
                g1.id: {
                    "id": g1.id,
                    "name": g1.name,
                    "connections": ["A"],
                }
            },
            "connections": {},
            "root_connections": [],
        },
    )
    assert service.get("A").group_id == g1.id


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_membership_mutations_emit_group_events(service):
    events = []
    service.add_listener(lambda e: events.append(e.kind))
    g1 = service.create_group("G1")
    a = _conn(service, "A")
    service.copy_connection_to_group(a.id, g1.id)
    service.remove_connection_from_group(a.id, g1.id)
    assert MutationKind.GROUP_ASSIGNED in events
    assert MutationKind.GROUP_REMOVED in events
