"""Regression tests for TerminalWidget.disconnect()'s status notification.

``disconnect()`` used to push the status change through the connection manager::

    GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', ...)

That dates from when ``window.connection_manager`` was a GObject
``ConnectionManager``. It is now a ``ConnectionPresentationStore`` — a read-only
DTO projection over daemon-authoritative state, with no ``emit`` — so the call
raised ``AttributeError`` out of ``disconnect()`` and aborted whatever was
closing the tab (observed closing a session from the tab-close dialog).

Status itself is owned by the daemon projection; the emit's only observable
effect was re-rendering the sidebar rows, which is what ``disconnect()`` asks
the window for now.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from sshpilot.connection_model import ConnectionState
from sshpilot.terminal import TerminalWidget


class _Store:
    """ConnectionPresentationStore stand-in: a projection, not a GObject."""

    def get_connections(self):
        return []


def _terminal(root, connection):
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.connection = connection
    terminal.connection_manager = _Store()
    terminal.get_root = lambda: root
    return terminal


def test_store_has_no_emit_so_the_legacy_push_would_crash():
    assert not hasattr(_Store(), "emit")


def test_disconnect_repaints_via_the_window_instead_of_emitting(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        "sshpilot.terminal.GLib.idle_add",
        lambda callback, *args: scheduled.append((callback, args)) or 1,
    )

    repainted = []
    connection = SimpleNamespace(nickname="host", is_connected=True)
    root = SimpleNamespace(
        on_connection_status_changed=lambda manager, conn, is_connected: repainted.append(
            (manager, conn, is_connected)
        )
    )
    terminal = _terminal(root, connection)

    terminal._repaint_connection_status(root)

    assert len(scheduled) == 1
    callback, args = scheduled[0]
    callback(*args)
    assert repainted == [(None, connection, False)]


def test_repaint_is_a_noop_without_a_window():
    terminal = _terminal(None, SimpleNamespace(nickname="host"))
    # An unrooted widget (window already gone) must not raise.
    assert terminal._repaint_connection_status(None) is None


def test_repaint_never_raises_out_of_disconnect(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("main loop is gone")

    monkeypatch.setattr("sshpilot.terminal.GLib.idle_add", _boom)

    root = SimpleNamespace(on_connection_status_changed=lambda *a: None)
    terminal = _terminal(root, SimpleNamespace(nickname="host"))

    assert terminal._repaint_connection_status(root) is None


def _exited_terminal(root):
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.connection = SimpleNamespace(hostname="example.test")
    terminal.connection_manager = _Store()
    terminal.connection_state = ConnectionState.CONNECTED
    terminal.connection_state_reason = ""
    terminal.is_connected = True
    terminal.last_error_message = None
    terminal.process_pid = None
    terminal.backend = None
    terminal._connect_grace_timer_id = None
    terminal.get_root = lambda: root
    terminal.emit = Mock()
    terminal._set_connecting_overlay_visible = Mock()
    terminal._record_error_detail = Mock()
    terminal._set_disconnected_banner_visible = Mock()
    return terminal


def test_child_exit_cleanup_accepts_read_only_presentation_store(monkeypatch):
    """An unexpected child exit must still reach its reconnect UI."""
    monkeypatch.setattr(
        "sshpilot.terminal.GLib.idle_add",
        lambda callback, *args: callback(*args),
    )
    terminal = _exited_terminal(None)
    terminal._classify_exit = Mock(
        return_value=(ConnectionState.DISCONNECTED, "Connection lost")
    )

    terminal._handle_child_exit_cleanup(256)

    assert terminal.connection_state is ConnectionState.DISCONNECTED
    assert terminal.is_connected is False
    terminal.emit.assert_called_once_with("connection-lost")
    terminal._set_disconnected_banner_visible.assert_called_once_with(
        True, "Connection lost"
    )


def test_clean_child_exit_accepts_read_only_presentation_store(monkeypatch):
    """A clean child exit must close its tab without a legacy state API."""
    monkeypatch.setattr(
        "sshpilot.terminal.GLib.idle_add",
        lambda callback, *args: callback(*args),
    )
    page = object()
    tab_view = SimpleNamespace(close_page=Mock(), get_page=Mock(return_value=page))
    root = SimpleNamespace(tab_view=tab_view)
    terminal = _exited_terminal(root)

    terminal._handle_child_exit_cleanup(0)

    assert terminal.connection_state is ConnectionState.DISCONNECTED
    assert terminal.is_connected is False
    tab_view.close_page.assert_called_once_with(page)
