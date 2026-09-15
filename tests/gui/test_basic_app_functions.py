"""Basic app functions, driven through the real main window.

Each test opens a terminal the way a user does and proves a shell really runs
by typing a command whose output differs from the typed text: the marker only
appears once the shell has evaluated ``$((6*7))``. Password login is covered by
``test_phase14_auth_dialogs.py`` and ``test_phase14_terminal_integration.py``.

Opt-in: SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui tests/gui/test_basic_app_functions.py
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

pytestmark = pytest.mark.gui

COMMAND = "printf 'SHELL_%s_OK\\n' $((6*7))\n"
MARKER = "SHELL_42_OK"


def _open_ssh_terminal(h, conn):
    """Start the connection from the window's terminal manager."""
    h.gui.window.terminal_manager.connect_to_host(conn, force_new=True)
    h.pump_until(
        lambda: h.find_terminal_widget(conn) is not None,
        timeout=20.0,
        label="terminal widget created",
    )


def _wait_for_output(h, term):
    """Wait for the marker; on timeout, say what the terminal actually shows."""
    from tests.gui._phase14_harness import Phase14EvidenceError

    try:
        return h.wait_for_marker(MARKER, terminal=term, timeout=30.0)
    except Phase14EvidenceError as error:
        backend = getattr(term, "backend", None)
        try:
            backend_text = backend.get_text() if backend is not None else None
        except Exception as exc:  # noqa: BLE001 - evidence only
            backend_text = f"<get_text failed: {exc!r}>"
        vte = getattr(term, "vte", None)
        vte_text = None
        if vte is not None:
            content = vte.get_text_format(Vte.Format.TEXT)
            vte_text = content[0] if isinstance(content, tuple) else content
        raise AssertionError(
            f"{error}; backend={type(backend).__name__} has_vte={vte is not None} "
            f"backend_text_tail={(backend_text or '')[-200:]!r} "
            f"vte_text_tail={(vte_text or '')[-200:]!r}"
        ) from error


def _run_command(h, term):
    h.pump_until(
        lambda: bool(getattr(term, "is_connected", False)),
        timeout=45.0,
        label="terminal connected",
    )
    h.ensure_input_ready(term, timeout=20.0)
    h.emit_terminal_input(COMMAND, term)
    return _wait_for_output(h, term)


def test_key_login_runs_a_remote_command(phase14_harness):
    h = phase14_harness
    h.stop_auth_helper()
    conn = h.add_key_connection("BasicKey", encrypted=False)

    _open_ssh_terminal(h, conn)

    assert MARKER in _run_command(h, h.find_terminal_widget(conn))


def test_passphrase_key_login_asks_and_runs_a_remote_command(phase14_harness):
    h = phase14_harness
    h.stop_auth_helper()
    conn = h.add_key_connection("BasicPassphraseKey", encrypted=True)

    _open_ssh_terminal(h, conn)
    dialog = h.wait_for_passphrase_dialog(timeout=45.0)
    h.respond_password_dialog(dialog, h.openssh.encrypted_key_passphrase)

    assert MARKER in _run_command(h, h.find_terminal_widget(conn))


def test_local_terminal_runs_a_shell_command(phase14_harness):
    h = phase14_harness
    win = h.gui.window
    before = set(win.terminal_to_connection)

    assert win.terminal_manager.show_local_terminal()
    h.pump_until(
        lambda: set(win.terminal_to_connection) - before,
        timeout=10.0,
        label="local terminal tab",
    )
    (term,) = set(win.terminal_to_connection) - before
    h.pump_until(
        lambda: bool(getattr(term, "process_pid", None)),
        timeout=20.0,
        label="local shell spawned",
    )

    term.feed_child_data(COMMAND.encode("utf-8"))

    assert MARKER in _wait_for_output(h, term)
