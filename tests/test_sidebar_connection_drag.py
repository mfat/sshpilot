"""Tests for sidebar connection drag-and-drop behaviour."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi.repository.Graphene")

from gi.repository import Gtk

from sshpilot.groups import GroupManager
from sshpilot.sidebar import _handle_connection_reorder, _on_connection_list_drop


class DummyConfig:
    """Simple in-memory config used for GroupManager tests."""

    def __init__(self):
        self._settings = {}

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value


class DummyRow:
    def __init__(self, nickname: str, index: int):
        self.connection = types.SimpleNamespace(nickname=nickname)
        self.group_id = None
        self.ungrouped_area = False
        self._index = index
        self.parent_list = None

    def get_allocation(self):
        return types.SimpleNamespace(y=self._index * 20, height=20)

    def get_index(self):
        return self._index

    def hide_drop_indicators(self):
        pass

    def show_drop_indicator(self, top: bool):
        pass

    def get_next_sibling(self):
        if not self.parent_list:
            return None
        next_index = self._index + 1
        return self.parent_list.get_row_at_index(next_index)

    def get_allocated_height(self):
        return 20


class DummyList:
    def __init__(self, rows):
        self._rows = rows
        self.selection_mode = None
        for row in self._rows:
            row.parent_list = self

    def get_row_at_y(self, y: int):
        for row in self._rows:
            alloc = row.get_allocation()
            if alloc.y <= y < alloc.y + alloc.height:
                return row
        return None

    def set_selection_mode(self, mode):
        self.selection_mode = mode

    def get_row_at_index(self, index: int):
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def get_first_child(self):
        return self._rows[0] if self._rows else None


class DummyWindow:
    def __init__(self, manager: GroupManager, rows):
        self.group_manager = manager
        self.connection_list = DummyList(rows)
        self._drop_indicator_row = None
        self._drop_indicator_position = None
        self._ungrouped_area_visible = False
        self._ungrouped_area_row = None
        self._connection_autoscroll_timeout_id = 0
        self._connection_autoscroll_velocity = 0.0
        self.connection_scrolled = None
        self._drag_in_progress = True
        self.rebuild_called = False

    def rebuild_connection_list(self):
        self.rebuild_called = True


def test_multi_connection_drop_reorders_all_selected():
    config = DummyConfig()
    manager = GroupManager(config)
    manager._save_groups = lambda: None  # type: ignore[attr-defined]

    manager.groups = {}
    manager.connections = {
        "conn_a": None,
        "conn_b": None,
        "conn_c": None,
        "conn_d": None,
    }
    manager.root_connections = ["conn_a", "conn_b", "conn_c", "conn_d"]

    rows = [DummyRow(nickname, idx) for idx, nickname in enumerate(manager.root_connections)]
    window = DummyWindow(manager, rows)

    payload = {
        "type": "connection",
        "connection_nickname": "conn_a",
        "connection_nicknames": ["conn_a", "conn_b"],
        "connections": [
            {"nickname": "conn_a", "index": 0},
            {"nickname": "conn_b", "index": 1},
        ],
    }

    target_row = rows[-1]
    allocation = target_row.get_allocation()
    drop_y = allocation.y + int(allocation.height * 0.75)

    result = _on_connection_list_drop(window, None, payload, 0.0, drop_y)

    assert result is True
    assert manager.root_connections == ["conn_c", "conn_d", "conn_a", "conn_b"]
    assert window.rebuild_called is True
    assert window.connection_list.selection_mode == Gtk.SelectionMode.MULTIPLE


def test_connection_reorder_moves_between_groups():
    config = DummyConfig()
    manager = GroupManager(config)
    manager._save_groups = lambda: None  # type: ignore[attr-defined]

    manager.groups = {
        "group_a": {
            "id": "group_a",
            "name": "Group A",
            "parent_id": None,
            "children": [],
            "connections": ["conn_a"],
            "expanded": True,
            "order": 0,
            "color": None,
        },
        "group_b": {
            "id": "group_b",
            "name": "Group B",
            "parent_id": None,
            "children": [],
            "connections": ["conn_b"],
            "expanded": True,
            "order": 1,
            "color": None,
        },
    }

    manager.connections = {
        "conn_a": "group_a",
        "conn_b": "group_b",
    }
    manager.root_connections = []

    rows = [DummyRow("conn_a", 0), DummyRow("conn_b", 1)]
    window = DummyWindow(manager, rows)

    target_row = rows[1]
    drop_y = target_row.get_allocated_height() * 0.25  # Drop above the target connection

    result = _handle_connection_reorder(
        window,
        target_row.connection.nickname,
        ["conn_a"],
        0.0,
        drop_y,
    )

    assert result is True
    assert manager.connections["conn_a"] == "group_b"
    assert manager.groups["group_a"]["connections"] == []
    assert manager.groups["group_b"]["connections"] == ["conn_a", "conn_b"]
    assert window.rebuild_called is True
