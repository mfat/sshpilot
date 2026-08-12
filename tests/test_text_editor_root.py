"""'Edit as root' routing: the privileged daemon file service drives sudo
reads/saves; the frontend never builds sudo commands. Pure-logic tests — no
host or GTK display needed."""
import os
import sys
import types
from concurrent.futures import Future

import pytest

import sshpilot.text_editor as text_editor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sshpilot.askpass_utils as askpass_utils  # noqa: F401
from sshpilot.askpass_utils import is_sudo_denied_error

pytest.importorskip("gi")

from sshpilot.text_editor import RemoteFileEditorWindow as Ed


def _bare_editor(*, session_pw=None, host="web", user="root"):
    """An editor instance without running __init__ (no GTK realize needed)."""
    del session_pw
    ed = Ed.__new__(Ed)
    ed._root_mode = False
    ed._sftp_manager = types.SimpleNamespace(host=host, username=user)
    return ed


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
    ed._on_saved = lambda: saved.append(True)
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
    ed._root_mode = False
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


def test_daemon_save_success_notifies_owner(monkeypatch, tmp_path):
    """The SSH config editor relies on this hook to invalidate cached
    effective-config warnings after the daemon writes ~/.ssh/config."""
    future = Future()
    future.set_result(types.SimpleNamespace(revision="new-revision"))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed, buffer, save_button, service, uploads = _daemon_save_editor(tmp_path, future)
    saved = []
    ed._on_saved = lambda: saved.append(True)

    ed._perform_save("new")

    assert service.text == "new"
    assert ed._daemon_file_revision == "new-revision"
    assert saved == [True]
    assert uploads == []
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


def _root_load_editor(tmp_path, future, *, privileged=True):
    class Service:
        privileged_supported = privileged

        def load_privileged(self):
            self.loaded = True
            return future

    class Button:
        def __init__(self):
            self.active = False

        def set_active(self, value):
            self.active = value

        def get_active(self):
            return self.active

        def add_css_class(self, _cls):
            pass

        def remove_css_class(self, _cls):
            pass

    ed = Ed.__new__(Ed)
    ed._temp_file = tmp_path / "config"
    ed._daemon_file_service = Service()
    ed._root_button = Button()
    ed._root_banner = None
    ed._root_mode = False
    ed._root_toggle_guard = False
    ed._daemon_file_revision = ""
    ed._show_toast = lambda *_args, **_kwargs: None
    ed._show_error = lambda *_args, **_kwargs: None
    ed._update_root_ui = lambda: None
    ed._load_file_content = lambda: setattr(ed, "loaded_content", True)
    ed._buffer = types.SimpleNamespace(
        get_start_iter=lambda: None,
        get_end_iter=lambda: None,
        get_text=lambda *_a: "",
        get_modified=lambda: False,
    )
    return ed


def test_root_read_routes_through_daemon_privileged_loader(monkeypatch, tmp_path):
    future = Future()
    future.set_result(types.SimpleNamespace(
        revision="root-rev", content="root content\n", exists=True,
        display_name="/etc/x", writable=True,
    ))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed = _root_load_editor(tmp_path, future)

    ed._begin_root_load()

    assert ed._root_mode is True
    assert ed._daemon_file_revision == "root-rev"
    assert ed._temp_file.read_text() == "root content\n"
    assert ed.loaded_content is True


def test_root_read_reports_unavailable_without_privileged_support(
    monkeypatch, tmp_path
):
    future = Future()
    future.set_result(types.SimpleNamespace(revision="r", content="x", exists=True))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed = _root_load_editor(tmp_path, future, privileged=False)
    ed._cancel_root_mode = lambda: setattr(ed, "_root_mode", False)

    ed._begin_root_load()

    assert ed._root_mode is False


def test_root_read_failure_cancels_root_mode(monkeypatch, tmp_path):
    from sshpilot.api.errors import ErrorCode, SshPilotError
    future = Future()
    future.set_exception(SshPilotError(
        ErrorCode.REMOTE_PERMISSION_DENIED, "The SFTP command failed"
    ))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed = _root_load_editor(tmp_path, future)
    ed._cancel_root_mode = lambda: setattr(ed, "_root_mode", False)

    ed._begin_root_load()

    assert ed._root_mode is False


def test_permission_denied_code_is_detected_without_message_markers():
    from sshpilot.api.errors import ErrorCode, SshPilotError
    exc = SshPilotError(ErrorCode.REMOTE_PERMISSION_DENIED, "The SFTP command failed")
    assert Ed._is_permission_denied_error(exc) is True
    assert Ed._is_permission_denied_error(ConnectionError("boom")) is False


def _bare_for_root_decision(*, is_local=False, manager_username=None):
    ed = Ed.__new__(Ed)
    ed._is_local = is_local
    ed._sftp_manager = (
        types.SimpleNamespace(username=manager_username)
        if manager_username is not None
        else None
    )
    return ed


@pytest.mark.parametrize("username,expected", [
    ("root", True),
    ("root ", True),
    ("alice", False),
    ("", False),
])
def test_remote_user_is_root(username, expected):
    ed = _bare_for_root_decision(manager_username=username)
    assert ed._remote_user_is_root() is expected


def test_remote_user_is_root_without_username_attribute():
    ed = Ed.__new__(Ed)
    ed._sftp_manager = types.SimpleNamespace(host="web")
    assert ed._remote_user_is_root() is False
    assert Ed._remote_username(None) == ""


def test_root_editing_applicable():
    assert _bare_for_root_decision(manager_username="alice")._root_editing_applicable() is True
    assert _bare_for_root_decision(manager_username="root")._root_editing_applicable() is False
    assert _bare_for_root_decision(manager_username=None)._root_editing_applicable() is False
    assert _bare_for_root_decision(is_local=True, manager_username="alice")._root_editing_applicable() is False


def test_root_read_missing_file_cancels_root_mode(monkeypatch, tmp_path):
    future = Future()
    future.set_result(types.SimpleNamespace(
        revision="r", content="", exists=False,
    ))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed = _root_load_editor(tmp_path, future)
    ed._cancel_root_mode = lambda: setattr(ed, "_root_mode", False)
    errors = []
    ed._show_error = lambda text: errors.append(text)

    ed._begin_root_load()

    assert ed._root_mode is False
    assert not hasattr(ed, "loaded_content")
    assert errors and "no longer exists" in errors[0]


def _root_save_editor(tmp_path, future, *, privileged=True):
    class Service:
        privileged_supported = privileged

        def save_text_privileged(self, text, *, make_backup):
            self.text = text
            self.make_backup = make_backup
            return future

    class Buffer:
        def __init__(self):
            self.modified = True

        def get_start_iter(self):
            return None

        def get_end_iter(self):
            return None

        def get_text(self, *_args):
            return "root edit"

        def set_modified(self, value):
            self.modified = value

    class SaveButton:
        def __init__(self):
            self.values = []

        def set_sensitive(self, value):
            self.values.append(value)

    service = Service()
    buffer = Buffer()
    save_button = SaveButton()
    ed = Ed.__new__(Ed)
    ed._temp_file = tmp_path / "config"
    ed._daemon_file_service = service
    ed._root_mode = True
    ed._buffer = buffer
    ed._save_button = save_button
    ed._gtksource_enabled = False
    ed._is_local = False
    ed._has_unsaved_changes = True
    ed._externally_modified = False
    ed._file_modified_time = 0.0
    ed._daemon_file_revision = "old-revision"
    ed._file_manager_window = None
    ed._update_title = lambda modified: setattr(ed, "title_modified", modified)
    ed._show_toast = lambda text, **_kwargs: setattr(ed, "toast", text)
    ed._show_error = lambda text: setattr(ed, "error", text)
    ed._sftp_manager = types.SimpleNamespace(
        upload=lambda *_args: setattr(ed, "used_legacy_upload", True)
    )
    ed._looks_permission_denied = lambda _error: False
    ed._offer_root_banner = lambda _action: False
    return ed, buffer, save_button, service


def test_root_save_routes_through_daemon_privileged_saver(monkeypatch, tmp_path):
    future = Future()
    future.set_result(types.SimpleNamespace(revision="new-root-rev"))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed, buffer, save_button, service = _root_save_editor(tmp_path, future)

    ed._upload_file_root()

    assert service.text == "root edit"
    assert service.make_backup is True
    assert not hasattr(ed, "used_legacy_upload")
    assert ed._daemon_file_revision == "new-root-rev"
    assert buffer.modified is False
    assert save_button.values[-1] is False
    assert ed.toast == "Saved successfully"


def test_root_save_reports_unavailable_without_privileged_support(
    monkeypatch, tmp_path
):
    future = Future()
    future.set_result(types.SimpleNamespace(revision="r"))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed, buffer, save_button, service = _root_save_editor(tmp_path, future, privileged=False)
    ed._buffer_set_modified = None

    ed._upload_file_root()

    assert not hasattr(service, "text")
    assert ed.error == "Edit as root is unavailable on this host."
    assert ed._has_unsaved_changes is True


def test_root_save_failure_keeps_dirty_state(monkeypatch, tmp_path):
    future = Future()
    future.set_exception(ConnectionError("daemon disconnected"))
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda fn, *args: fn(*args))
    ed, buffer, save_button, service = _root_save_editor(tmp_path, future)

    ed._upload_file_root()

    assert service.text == "root edit"
    assert ed.error == "Failed to save file: daemon disconnected"
    assert ed._has_unsaved_changes is True
    assert save_button.values[-1] is True
    assert "upload" not in ed.toast.lower()
