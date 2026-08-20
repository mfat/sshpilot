"""Focused unit tests for the macOS native menubar model.

The default suite stubs ``gi`` (see tests/conftest.py), so the builder is
injected with recording fakes that capture the menu structure exactly like
the real ``Gio.Menu`` / ``Gio.MenuItem`` API. The GUI test in
``test_gui_macos_menubar.py`` exercises the same model against real GIO.
"""

import types

from sshpilot.macos_menubar import build_macos_menubar, install_menubar


class _FakeMenu:
    def __init__(self):
        self.entries = []

    def append(self, label, action):
        self.entries.append(("item", label, action))

    def append_section(self, label, section):
        self.entries.append(("section", label, section))

    def append_submenu(self, label, submenu):
        self.entries.append(("submenu", label, submenu))

    def append_item(self, item):
        self.entries.append(("item-object", item))


class _FakeMenuItem:
    def __init__(self, label=None, submenu=None):
        self.label = label
        self.submenu = submenu
        self.attributes = {}

    @classmethod
    def new_submenu(cls, label, submenu):
        return cls(label, submenu)

    def set_attribute_value(self, name, value):
        self.attributes[name] = value


class _FakeVariant:
    def __init__(self, fmt, value):
        self.fmt = fmt
        self.value = value


def _flatten_items(menu):
    """Flatten item entries (including within sections) into (label, action)."""
    out = []
    for entry in menu.entries:
        kind = entry[0]
        if kind == "item":
            out.append((entry[1], entry[2]))
        elif kind in ("section", "submenu"):
            if isinstance(entry[2], _FakeMenu):
                out.extend(_flatten_items(entry[2]))
    return out


def _submenus_by_label(menu):
    result = {
        entry[1]: entry[2]
        for entry in menu.entries
        if entry[0] == "submenu"
    }
    for entry in menu.entries:
        if entry[0] == "item-object":
            item = entry[1]
            if item.submenu is not None:
                result[item.label] = item.submenu
    return result


def _build():
    return build_macos_menubar(
        menu_cls=_FakeMenu,
        menu_item_cls=_FakeMenuItem,
        variant_factory=_FakeVariant,
    )


def test_build_menubar_top_level_structure():
    model = _build()
    labels = [
        entry[1] if entry[0] == "submenu" else entry[1].label
        for entry in model.entries
        if entry[0] in ("submenu", "item-object")
    ]
    # The app menu (About/Settings…/Quit) is owned by the native macOS
    # application menu; the menubar model starts with File.
    assert labels == ["File", "Edit", "View", "Window", "Help"]


def test_build_menubar_uses_existing_actions():
    model = _build()
    menus = _submenus_by_label(model)

    assert [a for _l, a in _flatten_items(menus["File"])] == [
        "app.new-connection",
        "app.local-terminal",
        "win.save-session",
        "win.open-session",
        "win.import-config",
        "win.export-config",
    ]
    assert [a for _l, a in _flatten_items(menus["Edit"])] == [
        "text.undo",
        "text.redo",
        "clipboard.cut",
        "clipboard.copy",
        "clipboard.paste",
        "selection.select-all",
    ]
    assert [a for _l, a in _flatten_items(menus["View"])] == [
        "win.toggle_sidebar",
        "win.toggle-fullscreen",
        "app.tab-overview",
    ]
    assert [a for _l, a in _flatten_items(menus["Window"])] == [
        "app.tab-next",
        "app.tab-prev",
    ]
    assert [a for _l, a in _flatten_items(menus["Help"])] == [
        "app.shortcuts",
        "app.help",
        "win.check-for-updates",
        "win.report-problem",
    ]


def test_build_menubar_window_submenu_marked_macos_special():
    model = _build()
    window_items = [
        entry[1]
        for entry in model.entries
        if entry[0] == "item-object"
    ]
    assert len(window_items) == 1
    window_item = window_items[0]
    assert window_item.label == "Window"
    special = window_item.attributes.get("gtk-macos-special")
    assert special is not None
    assert special.fmt == "s"
    assert special.value == "window-submenu"


def test_build_menubar_file_sections_present():
    model = _build()
    file_menu = _submenus_by_label(model)["File"]
    section_labels = [
        entry[1]
        for entry in file_menu.entries
        if entry[0] == "section"
    ]
    assert section_labels == [None, None]


def _stub_gi_for_app_init(monkeypatch):
    """Stub the GI pieces SshPilotApplication.__init__ touches so it can run.

    The default suite stubs ``gi`` (tests/conftest.py), but the full
    ``SshPilotApplication.__init__`` still calls a handful of GIO entry
    points (SimpleAction construction, Application.__init__ kwargs, connect).
    Provide minimal fakes so the real ``__init__`` — including its menubar
    install timing — executes unchanged.
    """
    import gi.repository as repository
    import sshpilot.macos_menubar as mm

    class _FakeAction:
        def __init__(self, name, param_type=None):
            self.name = name

        def connect(self, *a):
            return 0

    class _FakeSimpleAction:
        @classmethod
        def new(cls, name, param_type=None):
            return _FakeAction(name, param_type)

    monkeypatch.setattr(repository.Gio, "SimpleAction", _FakeSimpleAction)
    monkeypatch.setattr(repository.Adw.Application, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(repository.Gio.Application, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(repository.Adw.Application, "do_startup", lambda self: None)
    monkeypatch.setattr(repository.Gio.Application, "do_startup", lambda self: None)
    monkeypatch.setattr(mm, "build_macos_menubar", lambda: "MENU")

    import sshpilot.main as main_module
    return main_module


def _make_app(main_module):
    """Create an SshPilotApplication instance and wire spy-able plumbing."""
    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.add_action = lambda a: None
    app.set_accels_for_action = lambda *a: None
    app.set_menubar = lambda model: app._installed.append(model)
    app.lookup_action = lambda name: None
    app.connect = lambda *a, **k: 0
    app._installed = []
    return app


def test_menubar_installed_at_startup_not_construction(monkeypatch):
    """RED: construction must not install the menubar; startup must, once.

    The buggy code called ``install_menubar`` from ``__init__`` (before
    GApplication registration, tripping GTK's
    ``gtk_application_set_menubar`` assertion). The correct lifecycle is:
    construct -> nothing installed; startup -> installed exactly once.
    """
    import sshpilot.macos_menubar as mm

    main_module = _stub_gi_for_app_init(monkeypatch)
    monkeypatch.setattr("sshpilot.main.is_macos", lambda: True)
    monkeypatch.setattr(mm, "is_macos", lambda: True)

    app = _make_app(main_module)
    app.__init__()

    # RED: the current code already installed during construction.
    assert app._installed == []

    app.do_startup()
    assert app._installed == ["MENU"]

    # Duplicate startup handling must not install a second time.
    app.do_startup()
    assert app._installed == ["MENU"]


def test_menubar_never_installed_off_macos(monkeypatch):
    """install_menubar must never be invoked when not on macOS."""
    import sshpilot.macos_menubar as mm

    main_module = _stub_gi_for_app_init(monkeypatch)
    monkeypatch.setattr("sshpilot.main.is_macos", lambda: False)

    install_spy = []
    monkeypatch.setattr(mm, "install_menubar", lambda app: install_spy.append(app))

    app = _make_app(main_module)
    app.__init__()
    app.do_startup()

    assert install_spy == []
    assert app._installed == []


def test_install_menubar_not_called_off_macos(monkeypatch):
    app = types.SimpleNamespace(
        set_menubar=lambda model: setattr(app, "_menubar", model),
    )
    monkeypatch.setattr("sshpilot.macos_menubar.is_macos", lambda: False)
    monkeypatch.setattr(
        "sshpilot.macos_menubar.build_macos_menubar",
        lambda: _FakeMenu(),
    )
    install_menubar(app)
    assert not hasattr(app, "_menubar")


def test_install_menubar_sets_model_on_macos(monkeypatch):
    app = types.SimpleNamespace(
        set_menubar=lambda model: setattr(app, "_menubar", model),
    )
    monkeypatch.setattr("sshpilot.macos_menubar.is_macos", lambda: True)
    monkeypatch.setattr(
        "sshpilot.macos_menubar.build_macos_menubar",
        lambda: _FakeMenu(),
    )
    install_menubar(app)
    assert isinstance(app._menubar, _FakeMenu)