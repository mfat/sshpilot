"""Activating a row: folders navigate, local files open, remote files ask."""

from types import SimpleNamespace


def _make_pane(module, *, remote):
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane._is_remote = remote
    pane._current_path = "/home/user"
    pane._entries = [
        SimpleNamespace(name="docs", is_dir=True),
        SimpleNamespace(name="report.txt", is_dir=False),
    ]
    pane.emitted = []
    pane.emit = lambda *args: pane.emitted.append(args)
    pane.get_root = lambda: "window"
    return pane


def _patch_opener(monkeypatch, module, *, fail=False):
    opened = []
    pane_module = __import__(module.FilePane.__module__, fromlist=["_"])

    def _open(path, *, parent=None, on_error=None):
        opened.append((path, parent))
        if fail:
            on_error(RuntimeError("no handler"))
        return True

    monkeypatch.setattr(pane_module, "open_with_default_app", _open)
    return opened


def test_local_file_opens_in_default_app(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    opened = _patch_opener(monkeypatch, module)
    pane = _make_pane(module, remote=False)

    pane._on_list_activate(None, 1)
    pane._on_grid_activate(None, 1)

    assert opened == [("/home/user/report.txt", "window")] * 2
    assert pane.emitted == []


def test_folder_still_navigates(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    opened = _patch_opener(monkeypatch, module)
    pane = _make_pane(module, remote=False)

    pane._on_list_activate(None, 0)

    assert pane.emitted == [("path-changed", "/home/user/docs")]
    assert opened == []


class _FakeAlertDialog:
    instances = []

    def __init__(self, heading, body):
        self.heading = heading
        self.body = body
        self.responses = []
        self.handler = None
        self.presented_on = None
        _FakeAlertDialog.instances.append(self)

    @classmethod
    def new(cls, heading, body):
        return cls(heading, body)

    def add_response(self, response_id, _label):
        self.responses.append(response_id)

    def set_response_appearance(self, *_args):
        pass

    def set_default_response(self, *_args):
        pass

    def set_close_response(self, *_args):
        pass

    def connect(self, _signal, handler):
        self.handler = handler

    def present(self, parent):
        self.presented_on = parent

    def respond(self, response_id):
        self.handler(self, response_id)


def _remote_pane_with_dialog(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    opened = _patch_opener(monkeypatch, module)
    pane_module = __import__(module.FilePane.__module__, fromlist=["_"])
    _FakeAlertDialog.instances = []
    monkeypatch.setattr(
        pane_module.Adw, "AlertDialog", _FakeAlertDialog, raising=False
    )
    monkeypatch.setattr(
        pane_module.Adw,
        "ResponseAppearance",
        SimpleNamespace(SUGGESTED="suggested"),
        raising=False,
    )
    pane = _make_pane(module, remote=True)
    pane.selected = []
    pane.calls = []
    pane._selection_model = SimpleNamespace(
        select_item=lambda position, exclusive: pane.selected.append((position, exclusive))
    )
    pane._on_menu_download = lambda: pane.calls.append("download")
    pane._on_menu_edit = lambda: pane.calls.append("edit")
    return pane, opened


def test_remote_file_offers_download_and_edit(load_file_manager_window, monkeypatch):
    pane, opened = _remote_pane_with_dialog(load_file_manager_window, monkeypatch)

    pane._on_list_activate(None, 1)

    (dialog,) = _FakeAlertDialog.instances
    assert dialog.heading == "report.txt"
    assert dialog.responses == ["cancel", "edit", "download"]
    assert dialog.presented_on is pane
    assert opened == []
    assert pane.emitted == []
    assert pane.calls == []


def test_remote_file_dialog_responses(load_file_manager_window, monkeypatch):
    pane, _opened = _remote_pane_with_dialog(load_file_manager_window, monkeypatch)

    for response in ("download", "edit", "cancel"):
        pane._on_grid_activate(None, 1)
        _FakeAlertDialog.instances[-1].respond(response)

    assert pane.calls == ["download", "edit"]
    assert pane.selected == [(1, True), (1, True)]


def test_remote_folder_still_navigates(load_file_manager_window, monkeypatch):
    pane, _opened = _remote_pane_with_dialog(load_file_manager_window, monkeypatch)

    pane._on_list_activate(None, 0)

    assert pane.emitted == [("path-changed", "/home/user/docs")]
    assert _FakeAlertDialog.instances == []


def test_failed_open_shows_toast(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    _patch_opener(monkeypatch, module, fail=True)
    pane = _make_pane(module, remote=False)
    toasts = []
    pane.show_toast = toasts.append

    pane._on_list_activate(None, 1)

    assert toasts == ["Could not open report.txt"]


def test_edit_is_offered_for_text_and_unknown_types_only(load_file_manager_window):
    module = load_file_manager_window()
    pane_module = __import__(module.FilePane.__module__, fromlist=["_"])
    editable = pane_module._is_editable_as_text

    for name in (
        "notes.txt", "sshd_config", "nginx.conf", "app.yaml", "main.py",
        "Makefile", ".bashrc", "id_ed25519.pub", "drawing.svg", "data.bin2",
        "script.ts", "README", "server.log",
    ):
        assert editable(name), name
    for name in (
        "photo.png", "photo.JPG", "song.mp3", "movie.mp4", "backup.zip",
        "backup.tar.gz", "manual.pdf", "report.docx", "font.ttf",
    ):
        assert not editable(name), name


def test_remote_non_text_file_offers_download_only(load_file_manager_window, monkeypatch):
    pane, _opened = _remote_pane_with_dialog(load_file_manager_window, monkeypatch)
    pane._entries[1] = SimpleNamespace(name="photo.png", is_dir=False)

    pane._on_list_activate(None, 1)

    (dialog,) = _FakeAlertDialog.instances
    assert dialog.responses == ["cancel", "download"]
    assert dialog.body == "Download the file to this computer?"


class _FakeListBox:
    def __init__(self):
        self.rows = []

    def get_first_child(self):
        return self.rows[0] if self.rows else None

    def remove(self, row):
        self.rows.remove(row)

    def append(self, row):
        self.rows.append(row)


class _FakeActionRow:
    def __init__(self, title):
        self.title = title

    def add_prefix(self, _icon):
        pass

    def set_activatable(self, _value):
        pass

    def connect(self, *_args):
        pass


def _context_menu_titles(load_file_manager_window, monkeypatch, *, remote, position):
    module = load_file_manager_window()
    pane_module = __import__(module.FilePane.__module__, fromlist=["_"])
    import sshpilot.icon_utils as icon_utils

    monkeypatch.setattr(pane_module.Gtk, "ListBox", _FakeListBox, raising=False)
    monkeypatch.setattr(pane_module.Adw, "ActionRow", _FakeActionRow, raising=False)
    monkeypatch.setattr(icon_utils, "new_image_from_icon_name", lambda _name: None)
    pane = _make_pane(module, remote=remote)
    listbox = _FakeListBox()
    widget = SimpleNamespace(grab_focus=lambda: None)
    pane._menu_popover = SimpleNamespace(
        get_child=lambda: listbox,
        get_parent=lambda: widget,
        set_pointing_to=lambda _rect: None,
        popup=lambda: None,
    )
    pane._update_menu_state = lambda: None
    pane.get_selected_entries = lambda: [pane._entries[position]]
    pane._menu_for_background = False
    pane._show_context_menu(widget, 0, 0)
    return [row.title for row in listbox.rows]


def test_local_file_menu_starts_with_open(load_file_manager_window, monkeypatch):
    titles = _context_menu_titles(load_file_manager_window, monkeypatch, remote=False, position=1)
    assert titles[0] == "Open"


def test_folder_menu_starts_with_open(load_file_manager_window, monkeypatch):
    titles = _context_menu_titles(load_file_manager_window, monkeypatch, remote=True, position=0)
    assert titles[0] == "Open"


def test_remote_file_menu_has_no_open(load_file_manager_window, monkeypatch):
    titles = _context_menu_titles(load_file_manager_window, monkeypatch, remote=True, position=1)
    assert "Open" not in titles
