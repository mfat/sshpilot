"""Stale DEC private modes must not survive into a new session (GH #1178).

An application that exits without restoring mouse tracking (``?1000``/``?1002``/
``?1003``) leaves the emulator routing drags to the remote instead of selecting,
so drag-select produces nothing and Ctrl+Shift+C silently copies an empty
string.  Resetting the emulator is what clears that mode.
"""
from unittest.mock import MagicMock

import pytest

from sshpilot.terminal_backends import PyXtermTerminalBackend


def _backend():
    b = object.__new__(PyXtermTerminalBackend)
    b.available = True
    b._webview = object()
    b._terminal_id = None          # the embedded backend never sets this
    b._pyxterm = None
    return b


def test_reset_is_not_a_no_op_without_a_terminal_id():
    """The old implementation keyed off ``_terminal_id``, which is never set,
    so every reset silently did nothing."""
    b = _backend()
    scripts = []
    b._run_javascript = scripts.append

    b.reset(False, False)

    assert scripts, "reset() must emit JavaScript, not silently do nothing"
    assert "term.reset()" in scripts[0]


def test_reset_clears_scrollback_only_when_asked():
    b = _backend()
    scripts = []
    b._run_javascript = scripts.append

    b.reset(False, True)
    assert "term.clear()" in scripts[0]

    scripts.clear()
    b.reset(False, False)
    assert "term.reset()" in scripts[0]
    assert "term.clear()" not in scripts[0]


def test_reset_is_inert_when_backend_unavailable():
    b = _backend()
    b.available = False
    scripts = []
    b._run_javascript = scripts.append

    b.reset(False, True)

    assert scripts == []


def test_reconnect_resets_the_reused_emulator(monkeypatch):
    """``reconnect_terminal`` reuses the widget, so it must reset the emulator
    before the replacement session starts."""
    from sshpilot import terminal_manager as tm

    backend = MagicMock()
    terminal = MagicMock()
    terminal.backend = backend
    terminal.connection = object()
    terminal.start_daemon_session.return_value = True

    order = []
    backend.reset.side_effect = lambda *a: order.append(("reset", a))
    terminal.start_daemon_session.side_effect = lambda *a, **k: (
        order.append(("start", a)) or True
    )

    manager = object.__new__(tm.TerminalManager)
    manager.window = MagicMock()
    manager._ensure_daemon_terminal_ready = lambda: MagicMock(ready=True)
    monkeypatch.setattr(tm, "connection_id_for", lambda c: "conn-1")

    assert manager.reconnect_terminal(terminal) is True

    kinds = [k for k, _ in order]
    assert "reset" in kinds, "reconnect must reset the reused emulator"
    assert kinds.index("reset") < kinds.index("start"), (
        "the reset has to happen before the new session starts"
    )
