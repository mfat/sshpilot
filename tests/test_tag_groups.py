"""Tests for the GTK-free virtual tag group helpers."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.tag_groups import (
    TAG_GROUP_ID_PREFIX,
    compute_tag_groups,
    is_tag_group_id,
    make_tag_group_info,
    tag_group_id,
)


class TestComputeTagGroups:
    def test_groups_and_sorts_tags_case_insensitively(self):
        result = compute_tag_groups({
            "a": ["zeta"],
            "b": ["Alpha"],
        })
        assert result == [("Alpha", ["b"]), ("zeta", ["a"])]

    def test_case_merge_first_seen_casing_wins(self):
        result = compute_tag_groups({
            "a": ["Prod"],
            "b": ["prod"],
        })
        assert result == [("Prod", ["a", "b"])]

    def test_members_sorted_case_insensitively_and_deduped(self):
        result = compute_tag_groups({
            "Zulu": ["web"],
            "alpha": ["web", "Web"],  # duplicate tag on one connection
        })
        assert result == [("web", ["alpha", "Zulu"])]

    def test_multi_tag_connection_in_each_group(self):
        result = compute_tag_groups({"srv": ["prod", "web"]})
        assert result == [("prod", ["srv"]), ("web", ["srv"])]

    def test_empty_and_whitespace_tags_ignored(self):
        assert compute_tag_groups({"a": ["", "  "], "b": [], "c": None}) == []
        assert compute_tag_groups({}) == []


class TestTagGroupIds:
    def test_tag_group_id_casefolds(self):
        assert tag_group_id("Prod") == "tag::prod"

    def test_is_tag_group_id(self):
        assert is_tag_group_id("tag::prod") is True
        assert is_tag_group_id("some-uuid") is False
        assert is_tag_group_id(None) is False
        assert is_tag_group_id(42) is False


class TestMakeTagGroupInfo:
    def test_shape(self):
        nicks = ["a", "b"]
        info = make_tag_group_info("Prod", nicks, expanded=False)
        assert info["id"] == "tag::prod"
        assert info["name"] == "Prod"
        assert info["tag_key"] == "prod"
        assert info["is_tag"] is True
        assert info["color"] is None
        assert info["parent_id"] is None
        assert info["children"] == []
        assert info["expanded"] is False
        assert info["connections"] == ["a", "b"]
        # connections is a copy — mutating it must not affect the input
        info["connections"].append("c")
        assert nicks == ["a", "b"]


class TestGroupManagerNoOpContract:
    """The sidebar relies on GroupManager safely ignoring synthetic tag ids."""

    def _make_manager(self):
        from sshpilot.groups import GroupManager

        class FakeConfig:
            def __init__(self):
                self.store = {}

            def get_setting(self, key, default=None):
                return self.store.get(key, default)

            def set_setting(self, key, value):
                self.store[key] = value

        return GroupManager(FakeConfig())

    def test_set_group_expanded_ignores_tag_id(self):
        gm = self._make_manager()
        before = dict(gm.groups)
        gm.set_group_expanded("tag::x", False)
        assert gm.groups == before

    def test_remove_connection_from_group_ignores_tag_id(self):
        gm = self._make_manager()
        gid = gm.create_group("real")
        gm.move_connection("srv", gid)
        gm.remove_connection_from_group("srv", "tag::x")
        assert "srv" in gm.groups[gid]["connections"]
