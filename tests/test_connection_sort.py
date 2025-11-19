"""Tests for connection sorting helpers."""

from __future__ import annotations

from typing import Any

from sshpilot.connection_sort import apply_connection_sort
from sshpilot.groups import GroupManager


class DummyConfig:
    """Minimal config shim for GroupManager."""

    def __init__(self):
        self._settings = {}

    def get_setting(self, key: str, default: Any = None):
        return self._settings.get(key, default)

    def set_setting(self, key: str, value: Any):
        self._settings[key] = value


class DummyConnection:
    def __init__(self, nickname: str, hostname: str = "", host: str = ""):
        self.nickname = nickname
        self.hostname = hostname
        self.host = host or nickname


def _build_manager() -> GroupManager:
    config = DummyConfig()
    manager = GroupManager(config)
    manager._save_groups = lambda: None  # type: ignore[attr-defined]
    return manager


def test_apply_connection_sort_changes_orders_and_is_idempotent():
    manager = _build_manager()
    manager.root_connections = ["beta", "alpha"]
    manager.groups = {
        "grp": {
            "id": "grp",
            "name": "Group 1",
            "parent_id": None,
            "children": [],
            "connections": ["delta", "gamma"],
            "expanded": True,
            "order": 0,
            "color": None,
        }
    }

    connections = [
        DummyConnection("alpha", hostname="a.example", host="alpha"),
        DummyConnection("beta", hostname="b.example", host="beta"),
        DummyConnection("gamma", hostname="c.example", host="gamma"),
        DummyConnection("delta", hostname="d.example", host="delta"),
    ]

    changed = apply_connection_sort(manager, connections, "name-asc")
    assert changed is True
    assert manager.root_connections == ["alpha", "beta"]
    assert manager.groups["grp"]["connections"] == ["delta", "gamma"]

    changed_again = apply_connection_sort(manager, connections, "name-asc")
    assert changed_again is False

    changed_desc = apply_connection_sort(manager, connections, "name-desc")
    assert changed_desc is True
    assert manager.root_connections == ["beta", "alpha"]
    assert manager.groups["grp"]["connections"] == ["gamma", "delta"]
