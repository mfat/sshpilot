"""Tests for the GTK-free virtual tag group helpers and their snapshot-backed
projection owner (ConnectionPresentationStore metadata + compute_tag_groups).

Tag groups are synthesized from immutable snapshot metadata at render time and
are never stored in GroupManager — the tags in connection metadata remain the
single source of truth.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.api.models.connection_store import (  # noqa: E402
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
)
from sshpilot.api.models.connections import (  # noqa: E402
    ConnectionId,
    ConnectionSummary,
)
from sshpilot.gtk.connection_store import ConnectionPresentationStore  # noqa: E402
from sshpilot.tag_groups import (  # noqa: E402
    add_tag_to_list,
    complete_tag_text,
    compute_tag_groups,
    is_tag_group_id,
    make_tag_group_info,
    migrate_expanded_state,
    rename_tag_in_list,
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


class TestCompleteTagText:
    KNOWN = ["db", "Prod", "production", "web"]

    def test_completes_first_match(self):
        # "Prod" is the first candidate matching "pr"; its canonical casing wins
        assert complete_tag_text("pr", 2, self.KNOWN) == ("Prod", 2)

    def test_completes_last_segment_only(self):
        assert complete_tag_text("web, pr", 7, self.KNOWN) == ("web, Prod", 7)

    def test_candidate_canonical_casing_wins(self):
        # The candidate's casing replaces the typed prefix: ssh Host aliases
        # are case-sensitive, so "usA"-style hybrids would break ProxyJump.
        assert complete_tag_text("PR", 2, self.KNOWN) == ("Prod", 2)
        assert complete_tag_text("us", 2, ["USA"]) == ("USA", 2)
        assert complete_tag_text("bastion1, us", 12, ["USA"]) == ("bastion1, USA", 12)

    def test_no_completion_mid_text(self):
        assert complete_tag_text("pr", 1, self.KNOWN) is None

    def test_exact_match_still_offers_longer_candidate(self):
        # "Prod" itself is skipped (nothing to add) but "production" extends
        # the prefix; typing ',' next replaces the selected suffix anyway.
        assert complete_tag_text("prod", 4, self.KNOWN) == ("production", 4)

    def test_exact_match_with_no_longer_candidate(self):
        assert complete_tag_text("web", 3, self.KNOWN) is None

    def test_skips_tags_already_in_entry(self):
        # "prod" already listed -> next candidate is "production"
        assert complete_tag_text("prod, pr", 8, self.KNOWN) == ("prod, production", 8)

    def test_no_match(self):
        assert complete_tag_text("xyz", 3, self.KNOWN) is None

    def test_empty_segment(self):
        assert complete_tag_text("web, ", 5, self.KNOWN) is None
        assert complete_tag_text("", 0, self.KNOWN) is None


class TestAddTagToList:
    def test_appends_new_tag(self):
        assert add_tag_to_list(["web"], "prod") == (["web", "prod"], True)

    def test_no_duplicate_case_insensitive(self):
        assert add_tag_to_list(["Prod"], "prod") == (["Prod"], False)

    def test_empty_tag_ignored(self):
        assert add_tag_to_list(["web"], "  ") == (["web"], False)

    def test_empty_or_none_list(self):
        assert add_tag_to_list([], "prod") == (["prod"], True)
        assert add_tag_to_list(None, "prod") == (["prod"], True)


class TestRenameTagInList:
    def test_basic_rename(self):
        assert rename_tag_in_list(["staging", "web"], "staging", "prod") == (["prod", "web"], True)

    def test_case_insensitive_match(self):
        assert rename_tag_in_list(["Prod"], "prod", "production") == (["production"], True)

    def test_merge_dedups_keeping_first_position(self):
        assert rename_tag_in_list(["staging", "web", "prod"], "staging", "prod") == (["prod", "web"], True)

    def test_unrelated_tags_untouched(self):
        assert rename_tag_in_list(["web", "db"], "staging", "prod") == (["web", "db"], False)

    def test_case_only_rename_reports_changed(self):
        assert rename_tag_in_list(["prod"], "prod", "Prod") == (["Prod"], True)

    def test_empty_input(self):
        assert rename_tag_in_list([], "a", "b") == ([], False)
        assert rename_tag_in_list(None, "a", "b") == ([], False)


class TestMigrateExpandedState:
    def test_moves_value(self):
        assert migrate_expanded_state({"staging": False}, "staging", "prod") == {"prod": False}

    def test_existing_new_key_wins_on_merge(self):
        state = {"staging": False, "prod": True}
        assert migrate_expanded_state(state, "staging", "prod") == {"prod": True}

    def test_missing_old_key_unchanged(self):
        assert migrate_expanded_state({"prod": True}, "staging", "prod") == {"prod": True}
        assert migrate_expanded_state({}, "a", "b") == {}

    def test_case_only_rename_keeps_state(self):
        # casefolded key is identical on a case-only rename
        assert migrate_expanded_state({"prod": False}, "prod", "prod") == {"prod": False}


class _SnapshotClient:
    def __init__(self, snapshot):
        self.server_instance_id = "daemon-tags"
        self._snapshot = snapshot

    def subscribe_events(self, callback):
        return type("_Sub", (), {"unsubscribe": lambda self: None})()

    def get_connection_store_snapshot(self):
        return self._snapshot


def _summary(cid):
    return ConnectionSummary(
        id=ConnectionId(cid), nickname=cid, host=cid,
        hostname=f"{cid}.example", username="u", port=22,
    )


def _snapshot(tags_by_id):
    """Build an immutable store snapshot whose metadata carries ``tags``."""
    connections = tuple(_summary(cid) for cid in tags_by_id)
    metadata = tuple(
        ConnectionMetadataSummary(connection_id=cid, values={"tags": tags})
        for cid, tags in tags_by_id.items()
        if tags
    )
    return ConnectionStoreSnapshot(
        generation=0,
        connections=connections,
        groups=(),
        root_connection_ids=tuple(tags_by_id.keys()),
        metadata=metadata,
    )


def _tag_map(store):
    """Same derivation the sidebar uses: per-connection metadata tags."""
    return {
        conn.nickname: list(store.get_metadata(conn.nickname).get("tags", []))
        for conn in store.connections
    }


class TestTagGroupProjection:
    """Virtual tag groups projected from immutable snapshot metadata."""

    def test_one_virtual_group_per_distinct_tag(self):
        store = ConnectionPresentationStore()
        store.attach_client(_SnapshotClient(_snapshot({
            "a": ["prod"],
            "b": ["prod", "web"],
        })))
        groups = compute_tag_groups(_tag_map(store))
        assert groups == [("prod", ["a", "b"]), ("web", ["b"])]

    def test_connection_appears_in_each_tag_group(self):
        store = ConnectionPresentationStore()
        store.attach_client(_SnapshotClient(_snapshot({"srv": ["prod", "web"]})))
        groups = compute_tag_groups(_tag_map(store))
        assert groups == [("prod", ["srv"]), ("web", ["srv"])]

    def test_duplicate_tags_do_not_duplicate_membership(self):
        store = ConnectionPresentationStore()
        store.attach_client(_SnapshotClient(_snapshot({"srv": ["prod", "PROD"]})))
        groups = compute_tag_groups(_tag_map(store))
        assert groups == [("prod", ["srv"])]

    def test_empty_and_whitespace_tags_ignored(self):
        store = ConnectionPresentationStore()
        store.attach_client(_SnapshotClient(_snapshot({
            "a": ["", "  "],
            "b": ["prod"],
        })))
        groups = compute_tag_groups(_tag_map(store))
        assert groups == [("prod", ["b"])]

    def test_tag_group_ordering_is_deterministic(self):
        store = ConnectionPresentationStore()
        store.attach_client(_SnapshotClient(_snapshot({
            "a": ["zeta"],
            "b": ["Alpha"],
        })))
        groups = compute_tag_groups(_tag_map(store))
        assert groups == [("Alpha", ["b"]), ("zeta", ["a"])]

    def test_snapshot_replacement_removes_obsolete_tag_groups(self):
        client = _SnapshotClient(_snapshot({
            "a": ["prod"],
            "b": ["web"],
        }))
        store = ConnectionPresentationStore()
        store.attach_client(client)
        assert compute_tag_groups(_tag_map(store)) == [("prod", ["a"]), ("web", ["b"])]

        client._snapshot = _snapshot({"a": []})  # tags removed daemon-side
        store.refresh()
        assert compute_tag_groups(_tag_map(store)) == []


class TestGroupManagerTagIdContract:
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


class TestComputeUntagged:
    def test_partitions_tagless_connections(self):
        from sshpilot.tag_groups import compute_untagged

        tag_map = {
            "tagged": ["prod"],
            "Zulu": [],
            "alpha": None,
            "blank": ["", "  "],
        }
        assert compute_untagged(tag_map) == ["alpha", "blank", "Zulu"]

    def test_empty_map(self):
        from sshpilot.tag_groups import compute_untagged

        assert compute_untagged({}) == []


class TestMakeUntaggedGroupInfo:
    def test_shape_and_flags(self):
        from sshpilot.tag_groups import (
            UNTAGGED_KEY,
            is_tag_group_id,
            make_untagged_group_info,
        )

        info = make_untagged_group_info("Untagged", ["a", "b"], True)
        assert info["name"] == "Untagged"
        assert info["connections"] == ["a", "b"]
        assert info["expanded"] is True
        assert info["untagged"] is True
        assert info["prefix"] == ""
        assert info["is_tag"] is True
        assert info["tag_key"] == UNTAGGED_KEY
        assert is_tag_group_id(info["id"])

    def test_key_cannot_collide_with_real_tag(self):
        from sshpilot.tag_groups import UNTAGGED_KEY, tag_group_id

        # A user tag named "untagged" must map to a different id/key.
        assert tag_group_id("untagged") != ("tag::" + UNTAGGED_KEY)
        assert "untagged" != UNTAGGED_KEY
