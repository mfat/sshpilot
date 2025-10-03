"""Integration tests for the sidebar drag-and-drop helpers."""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from sshpilot.groups import GroupManager
from sshpilot.sidebar import _handle_connection_reorder, setup_connection_list_dnd


class _DummyConfig:
    def __init__(self):
        self._settings = {}

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value


class _MockRow(Gtk.ListBoxRow):
    def __init__(self, nickname: str):
        super().__init__()
        self.connection = types.SimpleNamespace(nickname=nickname)

    def show_drop_indicator(self, *_args, **_kwargs):
        return None

    def hide_drop_indicators(self):
        return None


@pytest.mark.integration
def test_handle_connection_reorder_updates_group_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    Gtk.init([])

    if not hasattr(Gtk.ListBox, "__gtype__"):
        pytest.skip("GTK runtime not available")

    config = _DummyConfig()
    manager = GroupManager(config)
    manager._save_groups = lambda: None  # type: ignore[attr-defined]

    manager.groups = {
        "g1": {
            "id": "g1",
            "name": "Group 1",
            "parent_id": None,
            "children": [],
            "connections": ["c1", "c2"],
            "expanded": True,
            "order": 0,
            "color": None,
        }
    }
    manager.connections = {"c1": "g1", "c2": "g1", "c3": None}
    manager.root_connections = ["c3"]

    window = types.SimpleNamespace()
    window.group_manager = manager
    window.rebuild_connection_list = lambda: None

    window.connection_list = Gtk.ListBox()
    scrolled_candidate = Gtk.ScrolledWindow()
    try:
        scrolled_candidate.set_child(window.connection_list)
    except AttributeError:
        pytest.skip("GTK runtime not available")

    window.connection_scrolled = scrolled_candidate

    for nickname in ("c1", "c2", "c3"):
        row = _MockRow(nickname)
        row.get_allocated_height = lambda h=row: 20  # type: ignore[assignment]
        window.connection_list.append(row)

    setup_connection_list_dnd(window)

    assert _handle_connection_reorder(window, "c1", ["c2"], x=0.0, y=0.0) is True
    assert manager.groups["g1"]["connections"] == ["c2", "c1"]
    assert manager.connections["c2"] == "g1"
