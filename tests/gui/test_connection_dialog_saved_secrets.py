"""Add a connection in the New Connection dialog, then log in with what it saved.

A user types the host, user and port into the dialog, plus a password, or a
key file and its passphrase, and presses Save. Opening the connection must then
log in with no prompt: the saved secret is the only way in. A prompt that
opens anyway is answered wrongly, and the test fails on it.

The secrets go wherever the app keeps them on this system: in the Flatpak job,
through the Flatpak's secret portal.

Opt-in: SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui tests/gui/test_connection_dialog_saved_secrets.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests._gui_harness import requires_gui
from tests.gui._tab_proof import open_tab, prove_session_shell

Gtk, Adw, Gio, GLib = requires_gui()

pytestmark = pytest.mark.gui


def _visible_dialogs():
    from sshpilot.connection_dialog import ConnectionDialog

    return [
        w for w in Gtk.Window.list_toplevels()
        if isinstance(w, ConnectionDialog) and w.get_visible()
    ]


def _new_connection_dialog(h):
    """Open the dialog the window's Add button opens."""
    before = set(map(id, _visible_dialogs()))
    h.gui.window.on_add_connection_clicked(None)
    h.pump_until(
        lambda: [d for d in _visible_dialogs() if id(d) not in before],
        timeout=15.0,
        label="New Connection dialog",
    )
    return next(d for d in _visible_dialogs() if id(d) not in before)


def _trust_host_key(h) -> None:
    """Trust the fixture's host key in ~/.ssh/known_hosts, where ssh looks, so
    the only question ssh can ask is the secret under test.

    Before the dialog opens: the app watches that folder, and a change landing
    during Save reloads the configuration under it.
    """
    known = Path(os.environ["SSHPILOT_SSH_DIR"]) / "known_hosts"
    known.write_text(h.openssh.known_hosts.read_text())
    known.chmod(0o600)
    h.pump(1500)


def _fill_host(h, dialog, nickname: str) -> None:
    openssh = h.openssh
    dialog.nickname_row.set_text(nickname)
    dialog.hostname_row.set_text("127.0.0.1")
    dialog.port_row.set_text(str(openssh.port))
    dialog.username_row.set_text(openssh.username)


def _messages() -> list[str]:
    """What any open message dialog says (the dialog's errors open one)."""
    found = []
    for window in Gtk.Window.list_toplevels():
        if isinstance(window, Adw.MessageDialog) and window.get_visible():
            found.append(f"{window.get_heading()}: {window.get_body()}")
    return found


def _save(h, dialog, nickname: str):
    h.pump_until(dialog.effective_settled, timeout=15.0, label="OpenSSH answered the form")
    dialog.on_save_clicked()
    try:
        h.pump_until(lambda: not dialog.get_visible(), timeout=45.0, label="Save closed the dialog")
    except AssertionError as error:
        raise AssertionError(f"{error}; the app says: {_messages() or 'nothing'}") from error
    store = h.gui.window.connection_manager
    h.pump_until(
        lambda: store.find_connection_by_nickname(nickname) is not None,
        timeout=15.0,
        label=f"{nickname!r} in the sidebar",
    )
    return store.find_connection_by_nickname(nickname)


def _assert_not_in_config(secret: str) -> None:
    """The secret went to the secret store, not into the SSH config file."""
    for path in Path(os.environ["SSHPILOT_SSH_DIR"]).rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="replace"), f"secret written to {path}"


def _connect_without_prompt(h, connection, label: str) -> None:
    # Answer any prompt wrongly: only the saved secret may let ssh in.
    prompt = h.answer_password_dialog("not-the-saved-secret")
    term = open_tab(h, connection)
    prove_session_shell(h, term, label=label)
    assert not prompt["answered"], (
        f"{prompt['title']!r} asked although the dialog saved the secret"
    )


def test_saved_password_logs_in_without_asking(phase14_harness):
    h = phase14_harness
    h.stop_auth_helper()
    nickname = "DialogPassword"
    _trust_host_key(h)
    dialog = _new_connection_dialog(h)
    _fill_host(h, dialog, nickname)
    dialog.password_row.set_text(h.openssh.password)

    connection = _save(h, dialog, nickname)

    _assert_not_in_config(h.openssh.password)
    _connect_without_prompt(h, connection, "SSH with saved password")


def test_saved_key_passphrase_logs_in_without_asking(phase14_harness):
    h = phase14_harness
    h.stop_auth_helper()
    nickname = "DialogPassphrase"
    # A short path: OpenSSH cuts the key path in its passphrase question at
    # 100 characters, and the saved passphrase is found by that path
    # (pytest's own directory is longer).
    key = Path(os.environ["HOME"]) / "id_dialog"
    shutil.copyfile(h.openssh.encrypted_key_path, key)
    key.chmod(0o600)
    shutil.copyfile(f"{h.openssh.encrypted_key_path}.pub", f"{key}.pub")
    assert len(str(key)) < 100, key
    _trust_host_key(h)
    dialog = _new_connection_dialog(h)
    _fill_host(h, dialog, nickname)
    dialog.key_editor.add_path(str(key))
    rows = dialog.key_editor._passphrase_rows()
    assert len(rows) == 1, f"one key row with a passphrase field, got {rows}"
    entry, _path, _norm = rows[0]
    entry.set_text(h.openssh.encrypted_key_passphrase)

    connection = _save(h, dialog, nickname)

    _assert_not_in_config(h.openssh.encrypted_key_passphrase)
    _connect_without_prompt(h, connection, "SSH with saved key passphrase")
