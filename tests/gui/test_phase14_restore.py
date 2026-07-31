"""Phase 14 GTK terminal restore / replay / live continuation."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

try:
    import gi

    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: F401
except Exception as error:  # pragma: no cover
    pytest.skip(f"VTE unavailable: {error}", allow_module_level=True)

from tests.gui._phase14_harness import OUTPUT_MARKER

pytestmark = pytest.mark.gui


def test_restore_replay_and_live_output(phase14_harness):
    from sshpilot.api.models.sessions import SessionState
    from sshpilot.daemon_quit_policy import DaemonQuitDecision
    from sshpilot.daemon_session_restore import DaemonSessionRestoreManager

    h = phase14_harness
    conn = h.add_password_connection("P14Restore")
    term = h.connect_terminal(conn)
    h.emit_terminal_input(f"echo MARKER_A_{OUTPUT_MARKER}\n", term)
    h.wait_for_marker(f"MARKER_A_{OUTPUT_MARKER}", terminal=term)

    session_id = h.detach_terminal_keep_session(term)
    # Close GTK only; keep ephemeral daemon (do not stop_daemon).
    h.gui.app._daemon_quit_decision = DaemonQuitDecision.KEEP_RUNNING
    h.gui.window._daemon_quit_decision = DaemonQuitDecision.KEEP_RUNNING
    try:
        h.gui.shutdown()
    except Exception:
        pass
    h.gui = None

    # Session must still be live on the daemon.
    summaries = h.daemon_client.list_sessions()
    live = [s for s in summaries if str(s.id) == session_id]
    assert live, f"session missing after UI close: {summaries!r}"
    assert live[0].state in {SessionState.RUNNING, SessionState.STARTING}, live[0].state

    h.send_input_while_detached(
        session_id, f"echo MARKER_B_{OUTPUT_MARKER}\n"
    )

    h.restart_app()
    h.gui.app._daemon_quit_decision = DaemonQuitDecision.KEEP_RUNNING
    win = h.gui.window
    win.config.set_setting("terminal.daemon_restore_sessions", True)
    win.config.set_setting("terminal.daemon_auto_attach", True)
    h.start_auth_helper(submit_password=h.openssh.password)

    restore = DaemonSessionRestoreManager(win.config)
    restorable = restore.get_restorable_sessions(h.daemon_client)
    assert restorable, "no restorable sessions after restart"
    meta = next(m for m in restorable if str(m.session_id) == session_id)
    assert restore.restore_session(
        win, meta, h.daemon_client, win.client_bridge
    )
    h.pump(500)

    def _restored_connected():
        t = h.find_terminal_widget()
        if t is None or not getattr(t, "_daemon_mode", False):
            return False
        if getattr(t, "is_connected", False):
            return True
        ctrl = getattr(t, "_daemon_controller", None)
        if ctrl is None:
            return False
        try:
            from sshpilot.terminal_session_controller import TerminalSessionState

            return ctrl.state in {
                TerminalSessionState.ACTIVE,
                TerminalSessionState.REPLAYING,
            }
        except Exception:
            return False

    h.pump_until(
        _restored_connected, timeout=45.0, label="restored terminal connected"
    )
    term2 = h.find_terminal_widget()
    text = h.wait_for_marker(
        f"MARKER_B_{OUTPUT_MARKER}", terminal=term2, timeout=45.0
    )
    replay_ok = f"MARKER_B_{OUTPUT_MARKER}" in text

    # Ensure input ownership for live command.
    if not getattr(term2, "has_input_ownership", True):
        ctrl = term2._daemon_controller
        if hasattr(ctrl, "claim_input"):
            ctrl.claim_input()
            h.pump(300)

    h.emit_terminal_input(f"echo MARKER_C_{OUTPUT_MARKER}\n", term2)
    text2 = h.wait_for_marker(
        f"MARKER_C_{OUTPUT_MARKER}", terminal=term2, timeout=45.0
    )
    live_ok = f"MARKER_C_{OUTPUT_MARKER}" in text2
    duplicate = text2.count(f"MARKER_B_{OUTPUT_MARKER}") > 2

    tab = getattr(getattr(term2, "_daemon_controller", None), "tab_state", None)
    evidence = {
        "restored_terminal": True,
        "replay_ok": replay_ok,
        "live_output_ok": live_ok,
        "duplicate_output": duplicate,
        "session_id_preserved": str(getattr(tab, "session_id", None)) == session_id,
        "legacy_session_created": False,
    }
    assert evidence["replay_ok"], evidence
    assert evidence["live_output_ok"], evidence
    assert evidence["session_id_preserved"], evidence
    assert evidence["duplicate_output"] is False, evidence
