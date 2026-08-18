"""Phase 14 GTK quit-policy dialog (Keep running / Terminate / Cancel)."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

pytestmark = pytest.mark.gui


def test_quit_policy_dialog_responses(phase14_harness):
    from sshpilot.daemon_quit_policy import (
        DaemonQuitDecision,
        has_daemon_active_work,
        present_daemon_quit_dialog,
        resolve_quit_decision_from_policy,
    )

    h = phase14_harness
    win = h.gui.window
    win.config.set_setting("terminal.daemon_app_close_policy", "ask")
    assert resolve_quit_decision_from_policy(win.config) is None

    # Active daemon work via a real terminal (production path).
    conn = h.add_password_connection("P14Quit")
    assert type(win.client).__name__ == "DaemonClient"
    term = h.connect_terminal(conn)
    assert has_daemon_active_work(win) is True
    assert getattr(term, "is_connected", False) is True

    decisions = []

    def _capture(decision):
        decisions.append(decision)

    dialog = present_daemon_quit_dialog(win, on_decision=_capture)
    h.pump(200)
    assert dialog is not None
    dialog.emit("response", "cancel")
    h.pump(200)
    assert decisions == [DaemonQuitDecision.CANCEL]
    assert getattr(term, "is_connected", False) is True
    assert h.daemon_client.get_daemon_status().resources.sessions_active >= 1

    decisions.clear()
    dialog = present_daemon_quit_dialog(win, on_decision=_capture)
    h.pump(100)
    dialog.emit("response", "terminate")
    h.pump(100)
    assert decisions == [DaemonQuitDecision.TERMINATE_ALL]

    # There is no "keep running" outcome: quit ends the daemon and its work,
    # so any response other than Quit is a cancel.
    decisions.clear()
    dialog = present_daemon_quit_dialog(win, on_decision=_capture)
    h.pump(100)
    dialog.emit("response", "keep")
    h.pump(100)
    assert decisions == [DaemonQuitDecision.CANCEL]
