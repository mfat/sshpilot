"""Tests for pasteboard-safe internal DnD payloads.

macOS crashes when Gtk.DragSource advertises GObject.TYPE_PYOBJECT (see
GitHub #704/#847/#876). These tests pin the encode/decode contract used by
sidebar, split-view, and command-block drags.
"""

import json

from sshpilot.dnd_payload import (
    DND_FORMAT,
    decode_dnd_payload,
    encode_dnd_payload,
)
from sshpilot.split_view import _connections_from_drop_payload


def test_encode_wraps_payload_with_format():
    raw = encode_dnd_payload({"type": "group", "group_id": "g1"})
    container = json.loads(raw)
    assert container["format"] == DND_FORMAT
    assert container["payload"] == {"group_id": "g1", "type": "group"}


def test_decode_encoded_string():
    encoded = encode_dnd_payload({"type": "connection", "connection_nickname": "a"})
    assert decode_dnd_payload(encoded) == {
        "type": "connection",
        "connection_nickname": "a",
    }


def test_decode_bare_dict_for_tests():
    payload = {"type": "connection", "connection_nicknames": ["a"]}
    assert decode_dnd_payload(payload) == payload


def test_decode_rejects_garbage():
    assert decode_dnd_payload(None) is None
    assert decode_dnd_payload("not-json") is None
    assert decode_dnd_payload(42) is None
    assert decode_dnd_payload({"format": DND_FORMAT, "payload": "bad"}) is None
    assert decode_dnd_payload({"format": "other", "payload": {"type": "x"}}) is None
    assert decode_dnd_payload({"no": "type"}) is None


def test_connections_from_encoded_json_string():
    encoded = encode_dnd_payload(
        {"type": "connection", "connection_nicknames": ["a", "b"]}
    )
    assert _connections_from_drop_payload(encoded) == ["a", "b"]


def test_connections_from_encoded_group_returns_empty():
    encoded = encode_dnd_payload({"type": "group", "group_id": "g1"})
    assert _connections_from_drop_payload(encoded) == []


def test_connections_plain_string_still_empty():
    # Invalid / non-payload strings must not be treated as nicknames.
    assert _connections_from_drop_payload("nick") == []
