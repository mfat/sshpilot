"""Unit tests for the pure drag-and-drop helpers."""

from __future__ import annotations

import pytest

from sshpilot_dnd.logic import (
    AutoscrollParams,
    RowBounds,
    autoscroll_velocity,
    hit_test_insertion,
    reorder_connections_state,
)


def test_hit_test_handles_pointer_before_first_row():
    rows = [
        RowBounds("r0", top=10.0, height=20.0),
        RowBounds("r1", top=40.0, height=20.0),
    ]

    result = hit_test_insertion(rows, pointer_y=0.0)

    assert result is not None
    assert result.key == "r0"
    assert result.position == "above"


def test_hit_test_distinguishes_above_and_below_within_row():
    rows = [RowBounds("r0", top=0.0, height=20.0)]

    assert hit_test_insertion(rows, pointer_y=5.0).position == "above"
    assert hit_test_insertion(rows, pointer_y=15.0).position == "below"


def test_hit_test_chooses_last_row_when_pointer_below_all():
    rows = [
        RowBounds("r0", top=0.0, height=20.0),
        RowBounds("r1", top=30.0, height=20.0),
    ]

    result = hit_test_insertion(rows, pointer_y=100.0)

    assert result is not None
    assert result.key == "r1"
    assert result.position == "below"


@pytest.mark.parametrize(
    "pointer_y,expected",
    [
        (0.0, -5.0),
        (9.0, -0.5),
        (50.0, 0.0),
        (91.0, 0.5),
        (99.0, 4.5),
    ],
)
def test_autoscroll_velocity(pointer_y, expected):
    params = AutoscrollParams(
        viewport_height=100.0,
        pointer_y=pointer_y,
        margin=10.0,
        max_velocity=5.0,
    )

    velocity = autoscroll_velocity(params)

    assert pytest.approx(velocity, rel=1e-6) == expected


def test_reorder_within_same_group_above_target():
    connections = {"c1": "g1", "c2": "g1", "c3": None}
    group_connections = {
        "g1": ["c1", "c2"],
        None: ["c3"],
    }

    plan = reorder_connections_state(
        connection_to_group=connections,
        group_connections=group_connections,
        target_connection="c1",
        dragged_connections=["c2"],
        position="above",
    )

    assert plan.changed is True
    assert plan.group_connections["g1"] == ["c2", "c1"]


def test_reorder_moves_between_groups_and_appends():
    connections = {"c1": "g1", "c2": "g1", "c3": None}
    group_connections = {
        "g1": ["c1", "c2"],
        None: ["c3"],
    }

    plan = reorder_connections_state(
        connection_to_group=connections,
        group_connections=group_connections,
        target_connection="c3",
        dragged_connections=["c1"],
        position="below",
    )

    assert plan.group_connections["g1"] == ["c2"]
    assert plan.group_connections[None] == ["c3", "c1"]
    assert plan.connection_to_group["c1"] is None


def test_reorder_maintains_dragged_order():
    connections = {"c1": "g1", "c2": "g1", "c3": "g1", "c4": None}
    group_connections = {
        "g1": ["c1", "c2", "c3"],
        None: ["c4"],
    }

    plan = reorder_connections_state(
        connection_to_group=connections,
        group_connections=group_connections,
        target_connection="c3",
        dragged_connections=["c1", "c2"],
        position="below",
    )

    assert plan.group_connections["g1"] == ["c3", "c1", "c2"]
    assert plan.connection_to_group["c1"] == "g1"
    assert plan.connection_to_group["c2"] == "g1"
