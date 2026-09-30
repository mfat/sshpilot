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


class _FakeMenu:
    def __init__(self):
        self.items = []
        self.sections = []

    def append_item(self, item):
        self.items.append(item)

    def get_n_items(self):
        return len(self.items)

    def append_section(self, _label, section):
        self.sections.append([item.label for item in section.items])


class _FakeMenuItem:
    def __init__(self, label, action):
        self.label = label
        self.action = action
        self.attributes = {}

    @classmethod
    def new(cls, label, action):
        return cls(label, action)

    def set_attribute_value(self, key, value):
        self.attributes[key] = value


class _FakePopoverMenu:
    last = None

    def __init__(self, model):
        self.model = model
        self.parent = None
        self.pointing_to = None
        self.popped_up = False
        _FakePopoverMenu.last = self

    @classmethod
    def new_from_model(cls, model):
        return cls(model)

    def set_has_arrow(self, _value):
        pass

    def set_halign(self, _value):
        pass

    def connect(self, *_args):
        pass

    def set_parent(self, parent):
        self.parent = parent

    def set_pointing_to(self, rect):
        self.pointing_to = rect

    def popup(self):
        self.popped_up = True

    def popdown(self):
        pass


def _context_menu_sections(load_file_manager_window, monkeypatch, *, remote, position):
    module = load_file_manager_window()
    pane_module = __import__(module.FilePane.__module__, fromlist=["_"])
    monkeypatch.setattr(pane_module.Gio, "Menu", _FakeMenu, raising=False)
    monkeypatch.setattr(pane_module.Gio, "MenuItem", _FakeMenuItem, raising=False)
    monkeypatch.setattr(
        pane_module.GLib, "Variant", SimpleNamespace(new_string=lambda v: v), raising=False
    )
    monkeypatch.setattr(pane_module.Gtk, "PopoverMenu", _FakePopoverMenu, raising=False)
    monkeypatch.setattr(pane_module.Gtk, "Align", SimpleNamespace(START="start"), raising=False)
    monkeypatch.setattr(
        pane_module.Gdk, "Rectangle", lambda: SimpleNamespace(), raising=False
    )
    pane = _make_pane(module, remote=remote)
    pane._menu_popover = None
    pane._update_menu_state = lambda: None
    pane.get_selected_entries = lambda: [pane._entries[position]]
    pane._menu_for_background = False
    widget = SimpleNamespace(
        grab_focus=lambda: None,
        translate_coordinates=lambda _target, x, y: (x + 10, y + 20),
    )
    pane._show_context_menu(widget, 5, 7)
    popover = _FakePopoverMenu.last
    # Parented to the pane, not the view, pointing at the translated click.
    assert popover.parent is pane
    assert (popover.pointing_to.x, popover.pointing_to.y) == (15, 27)
    assert popover.popped_up
    assert pane._menu_popover is popover
    return popover.model.sections


def test_local_file_menu_starts_with_open(load_file_manager_window, monkeypatch):
    sections = _context_menu_sections(load_file_manager_window, monkeypatch, remote=False, position=1)
    assert sections == [
        ["Open", "Edit", "Upload…"],
        ["Cut", "Copy"],
        ["Rename…", "Delete"],
        ["Copy Location"],
        ["Properties…"],
    ]


def test_folder_menu_starts_with_open(load_file_manager_window, monkeypatch):
    sections = _context_menu_sections(load_file_manager_window, monkeypatch, remote=True, position=0)
    assert sections[0] == ["Open", "Download"]


def test_remote_file_menu_has_no_open(load_file_manager_window, monkeypatch):
    sections = _context_menu_sections(load_file_manager_window, monkeypatch, remote=True, position=1)
    assert sections[0] == ["Edit", "Download"]
