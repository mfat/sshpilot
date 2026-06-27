"""Tests for split-view connection drop handling.

The DnD payload parsing in SplitPane has been a repeatedly-broken surface, so
this pins down both the pure payload normalization
(_connections_from_drop_payload) and the routing in SplitPane._on_drop.

Scope: this verifies parsing + routing decisions against a fake SplitPane built
with __new__ (no real GTK). It does NOT exercise the live Gtk.DropTarget or any
actual widget creation — those remain manual.
"""

import types

from sshpilot.split_view import SplitPane, _connections_from_drop_payload


# ── pure payload normalization ──────────────────────────────────────────────

def test_payload_non_dict_returns_empty():
    assert _connections_from_drop_payload(None) == []
    assert _connections_from_drop_payload("nick") == []
    assert _connections_from_drop_payload(42) == []


def test_payload_wrong_or_missing_type_returns_empty():
    assert _connections_from_drop_payload({"connection_nicknames": ["a"]}) == []
    assert _connections_from_drop_payload({"type": "group", "connection_nicknames": ["a"]}) == []


def test_payload_nicknames_list_returned_in_order():
    payload = {"type": "connection", "connection_nicknames": ["a", "b", "c"]}
    assert _connections_from_drop_payload(payload) == ["a", "b", "c"]


def test_payload_single_nickname_fallback():
    payload = {"type": "connection", "connection_nickname": "solo"}
    assert _connections_from_drop_payload(payload) == ["solo"]


def test_payload_list_wins_over_single_fallback():
    payload = {
        "type": "connection",
        "connection_nicknames": ["a", "b"],
        "connection_nickname": "ignored",
    }
    assert _connections_from_drop_payload(payload) == ["a", "b"]


def test_payload_empty_connection_returns_empty():
    assert _connections_from_drop_payload({"type": "connection"}) == []
    assert _connections_from_drop_payload({"type": "connection", "connection_nicknames": []}) == []


# ── routing in _on_drop ─────────────────────────────────────────────────────

def _make_pane(known, terminal_count):
    """Build a fake SplitPane that records add_connection routing decisions."""
    pane = SplitPane.__new__(SplitPane)

    added_here = []
    added_in_new_pane = []

    def find(nick):
        return known.get(nick)

    pane._window = types.SimpleNamespace(
        connection_manager=types.SimpleNamespace(find_connection_by_nickname=find)
    )
    pane.get_terminal_count = lambda: terminal_count["value"]
    pane.add_connection = lambda conn: added_here.append(conn)

    new_pane = types.SimpleNamespace(add_connection=lambda conn: added_in_new_pane.append(conn))
    pane._split_view_tab = types.SimpleNamespace(add_pane=lambda: new_pane)

    return pane, added_here, added_in_new_pane


def test_drop_into_empty_pane_fills_in_place():
    conn = object()
    count = {"value": 0}
    pane, here, new = _make_pane({"a": conn}, count)

    assert pane._on_drop(None, {"type": "connection", "connection_nickname": "a"}, 0, 0) is True
    assert here == [conn]
    assert new == []


def test_drop_into_occupied_pane_spawns_new_pane():
    conn = object()
    count = {"value": 1}
    pane, here, new = _make_pane({"a": conn}, count)

    assert pane._on_drop(None, {"type": "connection", "connection_nickname": "a"}, 0, 0) is True
    assert here == []
    assert new == [conn]


def test_drop_multiple_first_fills_rest_open_new_panes():
    c1, c2, c3 = object(), object(), object()
    count = {"value": 0}
    pane, here, new = _make_pane({"a": c1, "b": c2, "c": c3}, count)
    # After the empty pane is filled, subsequent connections must open new panes.
    # The fake's get_terminal_count is dynamic so flip it once the first lands.
    real_add = pane.add_connection

    def add_and_occupy(conn):
        real_add(conn)
        count["value"] = 1

    pane.add_connection = add_and_occupy

    payload = {"type": "connection", "connection_nicknames": ["a", "b", "c"]}
    assert pane._on_drop(None, payload, 0, 0) is True
    assert here == [c1]
    assert new == [c2, c3]


def test_drop_unknown_nickname_is_skipped():
    count = {"value": 0}
    pane, here, new = _make_pane({}, count)  # nothing resolves

    assert pane._on_drop(None, {"type": "connection", "connection_nickname": "ghost"}, 0, 0) is True
    assert here == []
    assert new == []


def test_drop_non_connection_payload_returns_false():
    count = {"value": 0}
    pane, here, new = _make_pane({}, count)

    assert pane._on_drop(None, {"type": "group"}, 0, 0) is False
    assert here == []
    assert new == []
