"""Tests for Start-page sidebar drop → connect routing.

Pins WelcomePage._on_drop against a fake page built with __new__ (no real GTK).
Does not exercise Gtk.DropTarget itself.
"""

import types

from sshpilot.dnd_payload import encode_dnd_payload
from sshpilot.welcome_page import WelcomePage


def _make_page(known, *, groups=None):
    connected = []
    local = []
    batches = []

    page = WelcomePage.__new__(WelcomePage)
    page.window = types.SimpleNamespace(
        terminal_manager=types.SimpleNamespace(
            connect_to_host=lambda conn: connected.append(conn),
            show_local_terminal=lambda: local.append(True),
        ),
        group_manager=types.SimpleNamespace(groups=groups or {}),
        _open_connection_batch=lambda conns, title=None, prefer='split': batches.append(
            (list(conns), title, prefer)
        ),
    )
    page.connection_manager = types.SimpleNamespace(
        find_connection_by_nickname=lambda nick: known.get(nick),
    )
    page.remove_css_class = lambda *_a: None
    return page, connected, local, batches


def test_drop_connection_opens_host():
    conn = object()
    page, connected, local, batches = _make_page({"a": conn})

    assert page._on_drop(
        None, {"type": "connection", "connection_nickname": "a"}, 0, 0
    ) is True
    assert connected == [conn]
    assert local == []
    assert batches == []


def test_drop_connection_json_string_opens_hosts():
    c1, c2 = object(), object()
    page, connected, _local, _batches = _make_page({"a": c1, "b": c2})
    encoded = encode_dnd_payload(
        {"type": "connection", "connection_nicknames": ["a", "b"]}
    )

    assert page._on_drop(None, encoded, 0, 0) is True
    assert connected == [c1, c2]


def test_drop_unknown_nickname_returns_false():
    page, connected, _local, _batches = _make_page({})

    assert page._on_drop(
        None, {"type": "connection", "connection_nickname": "ghost"}, 0, 0
    ) is False
    assert connected == []


def test_drop_local_terminal_defers_open(monkeypatch):
    page, connected, local, _batches = _make_page({})
    scheduled = []
    monkeypatch.setattr(
        "sshpilot.welcome_page.GLib.idle_add",
        lambda cb, *a, **k: scheduled.append(cb) or 0,
    )

    assert page._on_drop(None, {"type": "local_terminal"}, 0, 0) is True
    assert local == []
    assert connected == []
    assert len(scheduled) == 1

    assert scheduled[0]() is False
    assert local == [True]


def test_drop_group_opens_as_tabs_batch():
    c1, c2 = object(), object()
    groups = {"g1": {"name": "Prod", "connections": ["a", "b", "missing"]}}
    page, connected, _local, batches = _make_page(
        {"a": c1, "b": c2}, groups=groups
    )

    assert page._on_drop(
        None, {"type": "group", "group_id": "g1"}, 0, 0
    ) is True
    assert connected == []
    assert batches == [([c1, c2], None, "tabs")]


def test_drop_non_sidebar_payload_returns_false():
    page, connected, local, batches = _make_page({"a": object()})

    assert page._on_drop(None, {"type": "command_block"}, 0, 0) is False
    assert connected == []
    assert local == []
    assert batches == []
