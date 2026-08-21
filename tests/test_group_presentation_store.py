"""Tests for GroupPresentationStore and GroupManager adapter.

Verifies that the presentation store reads exclusively from the connection
snapshot and that GroupManager correctly delegates to the mutation controller.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sshpilot.gtk.group_store import GroupPresentationStore
from sshpilot.groups import GroupManager


# ---------------------------------------------------------------------------
# GroupPresentationStore
# ---------------------------------------------------------------------------

class TestGroupPresentationStore:
    def _make(self, *, groups=None, root_ids=None):
        store = MagicMock()
        snapshot = MagicMock()
        snapshot.groups = groups or []
        snapshot.root_connection_ids = root_ids or []
        store.snapshot.return_value = snapshot
        store.groups = groups or []
        return GroupPresentationStore(store)

    def test_groups_property_delegates_to_connection_store(self):
        groups = [{"id": "g1", "name": "Prod"}]
        store = self._make(groups=groups)
        assert store.groups is groups

    def test_get_group_delegates(self):
        store = self._make()
        store.connection_store.get_group.return_value = {"id": "g1"}
        assert store.get_group("g1") == {"id": "g1"}
        store.connection_store.get_group.assert_called_once_with("g1")

    def test_get_connection_groups_delegates(self):
        store = self._make()
        store.connection_store.get_connection_groups.return_value = ["g1", "g2"]
        assert store.get_connection_groups("c1") == ["g1", "g2"]
        store.connection_store.get_connection_groups.assert_called_once_with("c1")


# ---------------------------------------------------------------------------
# GroupManager controller delegation
# ---------------------------------------------------------------------------

class TestGroupManagerProjection:
    def test_group_hierarchy_materializes_nested_group_dicts(self):
        gm = GroupManager.__new__(GroupManager)
        gm.groups = {
            "parent": {
                "id": "parent",
                "parent_id": None,
                "children": ["child"],
                "order": 0,
            },
            "child": {
                "id": "child",
                "parent_id": "parent",
                "children": [],
                "order": 0,
            },
        }
        hierarchy = gm.get_group_hierarchy()
        assert hierarchy[0]["id"] == "parent"
        assert hierarchy[0]["children"][0]["id"] == "child"
        assert gm.sibling_index("child") == ("parent", 0)

    def test_group_hierarchy_honors_presentation_order(self):
        gm = GroupManager.__new__(GroupManager)
        gm.groups = {
            "second": {
                "id": "second", "name": "Second", "parent_id": None,
                "children": [], "order": 1,
            },
            "first": {
                "id": "first", "name": "First", "parent_id": None,
                "children": [], "order": 0,
            },
        }

        assert [group["id"] for group in gm.get_group_hierarchy()] == [
            "first", "second",
        ]


class TestGroupManagerControllerDelegation:
    def _make(self, *, controller=None, client=None):
        gm = GroupManager.__new__(GroupManager)
        gm.client = client
        gm.controller = controller
        gm.config = None
        gm.groups = {}
        gm.connections = {}
        gm.root_connections = []
        gm._expanded = {}
        gm._projection_handler = None
        gm.connection_manager = MagicMock()
        gm.connection_manager.snapshot.return_value = None
        return gm

    def test_run_delegates_to_controller(self):
        ctrl = MagicMock()
        gm = self._make(controller=ctrl)
        gm.run(
            lambda: "result",
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        ctrl.run.assert_called_once()

    def test_run_sequence_delegates_to_controller(self):
        ctrl = MagicMock()
        gm = self._make(controller=ctrl)
        steps = [lambda _prev: "step1"]
        gm.run_sequence(
            steps,
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        ctrl.run_sequence.assert_called_once()

    def test_run_raises_without_controller(self):
        gm = self._make(controller=None)
        with pytest.raises(RuntimeError, match="no mutation controller"):
            gm.run(
                lambda: None,
                on_success=lambda r: None,
                on_error=lambda e: None,
            )

    def test_run_sequence_raises_without_controller(self):
        gm = self._make(controller=None)
        with pytest.raises(RuntimeError, match="no mutation controller"):
            gm.run_sequence(
                [lambda _prev: None],
                on_success=lambda r: None,
                on_error=lambda e: None,
            )

    def test_set_group_expanded_persists_to_config(self):
        config = MagicMock()
        gm = self._make()
        gm.config = config
        gm.set_group_expanded("g1", True)
        assert gm.is_group_expanded("g1") is True
        config.set_setting.assert_called_with("ui.group_expansion", {"g1": True})

    def test_set_group_expanded_updates_groups_dict(self):
        gm = self._make()
        gm.groups = {"g1": {"expanded": False}}
        gm.set_group_expanded("g1", True)
        assert gm.groups["g1"]["expanded"] is True

    def test_is_group_expanded_defaults_true(self):
        gm = self._make()
        assert gm.is_group_expanded("unknown") is True

    def test_load_expansion_from_config_reads_new_overlay(self):
        config = MagicMock()
        config.get_setting.return_value = {"g1": False, "g2": True}
        gm = self._make()
        gm.config = config
        gm._load_expansion_from_config()
        assert gm._expanded == {"g1": False, "g2": True}

    def test_load_expansion_from_config_migrates_legacy(self):
        config = MagicMock()
        # First call returns None for new overlay, second call returns legacy
        config.get_setting.side_effect = [
            None,  # ui.group_expansion
            {"groups": {"g1": {"expanded": True, "name": "Prod"}}},  # connection_groups
        ]
        gm = self._make()
        gm.config = config
        gm._load_expansion_from_config()
        assert gm._expanded == {"g1": True}
        config.set_setting.assert_called_with("ui.group_expansion", {"g1": True})

    def test_load_expansion_from_config_noop_when_config_none(self):
        gm = self._make()
        gm.config = None
        gm._load_expansion_from_config()  # should not raise
        assert gm._expanded == {}
