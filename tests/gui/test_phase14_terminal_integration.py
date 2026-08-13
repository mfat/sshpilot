"""Phase 14 production GTK terminal integration (real VTE + TerminalManager).

These tests exercise the user path:
MainWindow → TerminalManager.connect_to_host → daemon route → TerminalWidget
→ VTE feed/input/resize. They must not use GI stubs or disable VTE.
"""

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


def test_connect_via_terminal_manager_daemon_route(phase14_harness):
    h = phase14_harness
    conn = h.add_password_connection("P14Connect")
    term = h.connect_terminal(conn)
    evidence = h.terminal_evidence(term)
    assert evidence["gtk_connected"] is True, evidence
    assert evidence["terminal_widget_attached"] is True, evidence
    assert evidence["daemon_mode"] is True, evidence
    assert evidence["session_id"], evidence
    assert evidence["attachment_id"], evidence
    assert evidence["legacy_open_calls"] == [], evidence
    assert evidence["has_process_pid"] is False, evidence
    assert not evidence["critical_warnings"], evidence


def test_terminal_output_visible_in_vte(phase14_harness):
    h = phase14_harness
    conn = h.add_password_connection("P14Out")
    term = h.connect_terminal(conn)
    h.emit_terminal_input(f"echo {OUTPUT_MARKER}\n", term)
    text = h.wait_for_marker(OUTPUT_MARKER, terminal=term)
    evidence = h.terminal_evidence(term)
    evidence["terminal_output_visible"] = OUTPUT_MARKER in text
    evidence["output_marker"] = OUTPUT_MARKER
    assert evidence["terminal_output_visible"] is True, evidence
    assert evidence["gtk_connected"] is True, evidence


def test_terminal_input_verified(phase14_harness):
    h = phase14_harness
    conn = h.add_password_connection("P14In")
    term = h.connect_terminal(conn)
    ctrl = term._daemon_controller
    assert ctrl is not None
    assert getattr(term, "has_input_ownership", True)

    # Simple command
    h.emit_terminal_input(f"printf 'IN1-{OUTPUT_MARKER}\\n'\n", term)
    h.wait_for_marker(f"IN1-{OUTPUT_MARKER}", terminal=term)

    # UTF-8
    h.emit_terminal_input("printf 'café-π\\n'\n", term)
    h.wait_for_marker("café-π", terminal=term, timeout=20.0)

    # Rapid sequential
    for i in range(3):
        h.emit_terminal_input(f"printf 'R{i}\\n'\n", term)
    h.wait_for_marker("R2", terminal=term, timeout=20.0)

    # Large paste (bounded accept)
    blob = "X" * 4000
    h.emit_terminal_input(f"printf '%s\\n' {blob}\n", term)
    h.pump_until(
        lambda: blob[:32] in h.read_visible_terminal_text(term),
        timeout=25.0,
        label="large paste echo",
    )

    evidence = h.terminal_evidence(term)
    evidence["terminal_input_verified"] = True
    assert evidence["gtk_connected"] is True, evidence
    assert evidence["terminal_input_verified"] is True, evidence


def test_terminal_resize_verified(phase14_harness):
    h = phase14_harness
    conn = h.add_password_connection("P14Resize")
    term = h.connect_terminal(conn)
    vte = h.find_vte_widget(term)
    assert vte is not None
    initial_rows = max(1, int(vte.get_row_count() or 24))
    initial_cols = max(1, int(vte.get_column_count() or 80))

    h.resize_terminal(40, 100, term)
    h.pump(200)
    text = h.verify_daemon_resize(40, 100, term)
    final_rows = max(1, int(vte.get_row_count() or 0))
    final_cols = max(1, int(vte.get_column_count() or 0))

    evidence = h.terminal_evidence(term)
    evidence["terminal_resize_verified"] = "40 100" in text
    evidence["initial_size"] = f"{initial_rows}x{initial_cols}"
    evidence["final_size"] = f"{final_rows}x{final_cols}"
    evidence["stty_size"] = "40 100"
    assert evidence["terminal_resize_verified"] is True, evidence
    assert evidence["gtk_connected"] is True, evidence
