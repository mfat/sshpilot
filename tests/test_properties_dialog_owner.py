"""Owner/group display for the SFTP properties dialog."""

from sshpilot.file_manager.properties_dialog import PropertiesDialog


def test_format_owner_resolves_local_names():
    text = PropertiesDialog._format_owner(0, 0)
    assert " : " in text and text != "—"


def test_format_remote_owner_shows_numeric_ids():
    assert PropertiesDialog._format_remote_owner(1000, 1000) == "1000 : 1000"
    assert PropertiesDialog._format_remote_owner(0, 0) == "0 : 0"


def test_format_remote_owner_missing_ids_is_placeholder():
    assert PropertiesDialog._format_remote_owner(None, None) == "—"
    assert PropertiesDialog._format_remote_owner(1000, None) == "—"


class _Label:
    def __init__(self):
        self.text = None

    def set_label(self, text):
        self.text = text


def _remote_dialog(manager):
    from sshpilot.file_manager.common import FileEntry

    dialog = PropertiesDialog.__new__(PropertiesDialog)
    dialog._entry = FileEntry("data.bin", False, 12, 0.0, None)
    dialog._current_path = "/srv"
    dialog._sftp_manager = manager
    return dialog


def test_remote_free_space_is_appended_to_the_summary(monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from sshpilot.file_manager import properties_dialog

    monkeypatch.setattr(properties_dialog.GLib, "idle_add", lambda fn: fn(), raising=False)
    asked = []

    class Manager:
        def filesystem_usage(self, path):
            asked.append(path)
            future = Future()
            future.set_result(SimpleNamespace(available_bytes=3 * 1024 * 1024))
            return future

    label = _Label()
    _remote_dialog(Manager())._start_remote_free_space(label, ["12 bytes"])

    assert asked == ["/srv/data.bin"]
    assert label.text == "12 bytes — " + properties_dialog._free_space_text(3 * 1024 * 1024)


def test_remote_free_space_is_left_out_when_the_server_cannot_say(monkeypatch):
    from concurrent.futures import Future

    from sshpilot.file_manager import properties_dialog

    monkeypatch.setattr(properties_dialog.GLib, "idle_add", lambda fn: fn(), raising=False)

    class Manager:
        def filesystem_usage(self, path):
            future = Future()
            future.set_exception(OSError("unsupported"))
            return future

    label = _Label()
    _remote_dialog(Manager())._start_remote_free_space(label, ["12 bytes"])

    assert label.text is None
