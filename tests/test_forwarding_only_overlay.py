"""Forwarding-only overlay mode on TerminalWidget (no live VTE required)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from sshpilot.connection_model import ConnectionState  # noqa: E402
from sshpilot.terminal import TerminalWidget  # noqa: E402


class _FakeBox:
    def __init__(self):
        self._children = []

    def get_first_child(self):
        return self._children[0] if self._children else None

    def append(self, child):
        self._children.append(child)

    def remove(self, child):
        self._children.remove(child)


class _RowStub:
    """Stand-in for a Gtk widget row that supports sibling traversal."""

    def __init__(self, box):
        self._box = box

    def get_next_sibling(self):
        try:
            idx = self._box._children.index(self)
        except ValueError:
            return None
        nxt = idx + 1
        if nxt >= len(self._box._children):
            return None
        return self._box._children[nxt]


def _bare_terminal(connection):
    term = TerminalWidget.__new__(TerminalWidget)
    term.connection = connection
    term.is_connected = False
    term.connection_state = ConnectionState.UNKNOWN
    term._daemon_mode = False
    term._daemon_controller = None
    term._forwarding_only_known = None
    term._overlay_mode = "connecting"
    term.connecting_bg = Mock()
    term.connecting_box = Mock()
    term.forwarding_box = Mock()
    term.forwarding_subtitle = Mock()
    term.forwarding_hint = Mock()
    box = _FakeBox()
    real_append = box.append

    def append(_child):
        real_append(_RowStub(box))

    box.append = append  # type: ignore[method-assign]
    term.forwarding_rules_box = box
    return term


def _stub_gtk_rows(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.terminal.Gtk.Box",
        lambda *a, **k: SimpleNamespace(
            set_halign=lambda *_: None,
            set_hexpand=lambda *_: None,
            append=lambda *_: None,
        ),
    )
    monkeypatch.setattr(
        "sshpilot.terminal.Gtk.Label",
        lambda *a, **k: SimpleNamespace(
            add_css_class=lambda *_: None,
            set_width_chars=lambda *_: None,
            set_halign=lambda *_: None,
            set_xalign=lambda *_: None,
            set_wrap=lambda *_: None,
            set_justify=lambda *_: None,
        ),
    )
    monkeypatch.setattr(
        "sshpilot.terminal.Gtk.Orientation",
        SimpleNamespace(HORIZONTAL=0, VERTICAL=1),
        raising=False,
    )
    monkeypatch.setattr(
        "sshpilot.terminal.Gtk.Align",
        SimpleNamespace(START=0, FILL=1, CENTER=2),
        raising=False,
    )
    monkeypatch.setattr(
        "sshpilot.terminal.Gtk.Justification",
        SimpleNamespace(CENTER=0),
        raising=False,
    )


def test_post_connect_shows_forwarding_overlay_after_status_evidence(monkeypatch):
    conn = SimpleNamespace(
        forwarding_only=True,
        forwarding_rules=(
            {
                "type": "local",
                "listen_addr": "127.0.0.1",
                "listen_port": 8080,
                "remote_host": "127.0.0.1",
                "remote_port": 80,
                "enabled": True,
            },
        ),
        display_name="prod-db",
        nickname="prod-db",
    )
    term = _bare_terminal(conn)
    term.is_connected = True
    term.connection_state = ConnectionState.CONNECTED
    _stub_gtk_rows(monkeypatch)

    term._show_post_connect_overlay()
    assert term._overlay_mode == "forwarding"
    term.connecting_bg.set_visible.assert_called_with(True)
    term.connecting_box.set_visible.assert_called_with(False)
    term.forwarding_box.set_visible.assert_called_with(True)
    assert term.forwarding_rules_box.get_first_child() is not None


def test_forwarding_overlay_waits_for_daemon_session_running(monkeypatch):
    """Sidebar CONNECTED uses SessionState.RUNNING — overlay must wait too."""
    conn = SimpleNamespace(
        forwarding_only=True,
        forwarding_rules=(),
        display_name="prod-db",
        nickname="prod-db",
    )
    term = _bare_terminal(conn)
    term._daemon_mode = True
    term._daemon_controller = SimpleNamespace(session_running=False)
    term._forwarding_only_known = True
    _stub_gtk_rows(monkeypatch)

    term._show_post_connect_overlay()
    assert term._overlay_mode == "connecting"

    term._daemon_controller.session_running = True
    term._show_post_connect_overlay()
    assert term._overlay_mode == "forwarding"


def test_post_connect_clears_overlay_for_normal_shell():
    conn = SimpleNamespace(forwarding_only=False, nickname="shell")
    term = _bare_terminal(conn)
    term.is_connected = True
    term.connection_state = ConnectionState.CONNECTED
    term._show_post_connect_overlay()
    assert term._overlay_mode == "none"
    term.connecting_bg.set_visible.assert_called_with(False)
    term.forwarding_box.set_visible.assert_called_with(False)


def test_post_connect_keeps_connecting_while_unresolved():
    term = _bare_terminal(SimpleNamespace(nickname="maybe"))
    term.is_connected = True
    term.connection_state = ConnectionState.CONNECTED
    term._show_post_connect_overlay()
    assert term._overlay_mode == "connecting"
    term.connecting_box.set_visible.assert_called_with(True)


def test_connecting_mode_hides_forwarding_box():
    term = _bare_terminal(SimpleNamespace(forwarding_only=True, nickname="x"))
    term._set_connecting_overlay_visible(True)
    assert term._overlay_mode == "connecting"
    term.forwarding_box.set_visible.assert_called_with(False)
    term.connecting_box.set_visible.assert_called_with(True)
