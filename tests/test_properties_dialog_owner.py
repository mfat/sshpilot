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
        self.visible = False

    def set_label(self, text):
        self.text = text

    def set_visible(self, visible):
        self.visible = visible


def _remote_dialog(manager, *, is_dir=True):
    from sshpilot.file_manager.common import FileEntry

    dialog = PropertiesDialog.__new__(PropertiesDialog)
    dialog._entry = FileEntry("docs" if is_dir else "data.bin", is_dir, 12, 0.0, None)
    dialog._entries = [dialog._entry]
    dialog._current_path = "/srv"
    dialog._sftp_manager = manager
    dialog._free_space_label = _Label()
    return dialog


def test_remote_free_space_fills_its_own_caption(monkeypatch):
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

    dialog = _remote_dialog(Manager())
    dialog._start_remote_free_space()

    assert asked == ["/srv/docs"]
    assert dialog._free_space_label.text == properties_dialog._free_space_text(3 * 1024 * 1024)
    assert dialog._free_space_label.visible is True


def test_remote_free_space_is_left_out_when_the_server_cannot_say(monkeypatch):
    from concurrent.futures import Future

    from sshpilot.file_manager import properties_dialog

    monkeypatch.setattr(properties_dialog.GLib, "idle_add", lambda fn: fn(), raising=False)

    class Manager:
        def filesystem_usage(self, path):
            future = Future()
            future.set_exception(OSError("unsupported"))
            return future

    dialog = _remote_dialog(Manager())
    dialog._start_remote_free_space()

    assert dialog._free_space_label.text is None
    assert dialog._free_space_label.visible is False
