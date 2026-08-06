"""'Edit as root' command builders (sudo cat / sudo tee) and the sudo-denial
classifier they rely on. Pure-logic tests — no host or GTK display needed."""
import os
import sys
import types
from concurrent.futures import Future

import pytest

import sshpilot.text_editor as text_editor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sshpilot.askpass_utils as askpass_utils
from sshpilot.askpass_utils import is_sudo_denied_error

pytest.importorskip("gi")

from sshpilot.text_editor import RemoteFileEditorWindow as Ed


def _bare_editor(*, session_pw=None, host="web", user="root"):
    """An editor instance without running __init__ (no GTK realize needed)."""
    ed = Ed.__new__(Ed)
    ed._sudo_password = session_pw
    ed._sftp_manager = types.SimpleNamespace(host=host, username=user)
    return ed


def test_read_cmd_with_password():
    assert Ed._root_read_cmd("/etc/hosts", has_pw=True) == \
        "sudo -S -p '' -- cat -- /etc/hosts"


def test_read_cmd_passwordless():
    assert Ed._root_read_cmd("/etc/hosts", has_pw=False) == \
        "sudo -n -- cat -- /etc/hosts"


def test_write_cmd_with_password():
    assert Ed._root_write_cmd("/etc/hosts", has_pw=True) == \
        "sudo -S -p '' -- tee -- /etc/hosts > /dev/null"


def test_write_cmd_passwordless():
    assert Ed._root_write_cmd("/etc/hosts", has_pw=False) == \
        "sudo -n -- tee -- /etc/hosts > /dev/null"


def test_cmd_quotes_paths_with_spaces_and_dashes():
    # shlex.quote guards spaces; the leading `--` guards dash-prefixed paths.
    assert Ed._root_read_cmd("/tmp/a b", has_pw=False) == \
        "sudo -n -- cat -- '/tmp/a b'"
    assert "-- -weird" in Ed._root_write_cmd("-weird", has_pw=False)


def test_resolve_root_pw_prefers_session(monkeypatch):
    monkeypatch.setattr(askpass_utils, "lookup_sudo_password",
                        lambda h, u: "from-keyring")
    ed = _bare_editor(session_pw="from-session")
    assert ed._resolve_root_pw() == ("from-session", False)


def test_resolve_root_pw_falls_back_to_keyring(monkeypatch):
    monkeypatch.setattr(askpass_utils, "lookup_sudo_password",
                        lambda h, u: "from-keyring")
    ed = _bare_editor(session_pw=None)
    assert ed._resolve_root_pw() == ("from-keyring", True)


def test_resolve_root_pw_none_when_nothing_stored(monkeypatch):
    monkeypatch.setattr(askpass_utils, "lookup_sudo_password", lambda h, u: "")
    ed = _bare_editor(session_pw=None)
    assert ed._resolve_root_pw() == (None, False)


def test_daemon_backed_editor_uses_local_loading_label():
    assert Ed._initial_loading_message(is_local=False, daemon_file_service=object()) == "Loading…"


def test_sftp_editor_keeps_downloading_loading_label():
    assert Ed._initial_loading_message(is_local=False, daemon_file_service=None) == "Downloading…"


def test_local_save_notifies_owner(tmp_path):
    saved = []
    ed = Ed.__new__(Ed)
    ed._temp_file = tmp_path / "config"
    ed._temp_file.write_text("old")
    ed._buffer = types.SimpleNamespace(set_modified=lambda _value: None)
    ed._gtksource_enabled = False
    ed._is_local = True
    ed._daemon_file_service = None
    ed._on_local_saved = lambda: saved.append(True)
    ed._update_title = lambda _modified: None
    ed._show_toast = lambda *_args, **_kwargs: None
    ed._save_button = types.SimpleNamespace(set_sensitive=lambda _value: None)
    ed._file_manager_window = None

    ed._perform_save("new")

    assert ed._temp_file.read_text() == "new"
    assert saved == [True]


def _daemon_save_editor(tmp_path, future):
    class Buffer:
        def __init__(self):
            self.modified = True
            self.modified_values = []

        def set_modified(self, value):
            self.modified = value
            self.modified_values.append(value)

    class SaveButton:
        def __init__(self):
            self.values = []

        def set_sensitive(self, value):
            self.values.append(value)

    class Service:
        def save_text(self, text, *, make_backup):
            self.text = text
            self.make_backup = make_backup
            return future

    uploads = []
    buffer = Buffer()
    save_button = SaveButton()
    service = Service()
    ed = Ed.__new__(Ed)
    ed._temp_file = tmp_path / "config"
    ed._temp_file.write_text("old")
    ed._buffer = buffer
    ed._gtksource_enabled = False
    ed._is_local = False
    ed._daemon_file_service = service
    ed._save_button = save_button
    ed._file_manager_window = None
    ed._update_title = lambda modified: setattr(ed, "title_modified", modified)
    ed._show_toast = lambda text, **_kwargs: setattr(ed, "toast", text)
    ed._show_error = lambda text: setattr(ed, "error", text)
    ed._sftp_manager = types.SimpleNamespace(
        upload=lambda *_args: uploads.append(True)
    )
    ed._looks_permission_denied = lambda _error: False
    ed._offer_root_banner = lambda _action: False
    ed._has_unsaved_changes = True
    ed._externally_modified = False
    ed._file_modified_time = 0.0
    ed._daemon_file_revision = "old-revision"
    return ed, buffer, save_button, service, uploads


def test_daemon_save_failure_preserves_dirty_state_and_uses_save_error(
    monkeypatch, tmp_path
):
    future = Future()
    future.set_exception(ConnectionError("daemon disconnected"))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed, buffer, save_button, service, uploads = _daemon_save_editor(tmp_path, future)

    ed._perform_save("new")

    assert service.text == "new"
    assert buffer.modified is True
    assert buffer.modified_values[-1] is True
    assert ed._has_unsaved_changes is True
    assert save_button.values[-1] is True
    assert "upload" not in ed.toast.lower()
    assert ed.error == "Failed to save file: daemon disconnected"
    assert uploads == []


def test_daemon_save_success_clears_dirty_state_with_save_message(
    monkeypatch, tmp_path
):
    future = Future()
    future.set_result(types.SimpleNamespace(revision="new-revision"))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed, buffer, save_button, service, uploads = _daemon_save_editor(tmp_path, future)

    ed._perform_save("new")

    assert service.text == "new"
    assert ed._daemon_file_revision == "new-revision"
    assert buffer.modified is False
    assert ed._has_unsaved_changes is False
    assert save_button.values[-1] is False
    assert ed.toast == "Saved successfully"
    assert uploads == []


@pytest.mark.parametrize("text,expected", [
    ("webuser is not in the sudoers file", True),
    ("user is not allowed to execute '/usr/bin/tee'", True),
    ("sudo: unknown user: ghost", True),
    ("sudo: a password is required", False),
    ("Sorry, try again.", False),
    ("", False),
])
def test_is_sudo_denied_error(text, expected):
    assert is_sudo_denied_error(text) is expected
