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
    assert labels == ["SSH Pilot", "File", "Edit", "View", "Window", "Help"]


def test_build_menubar_uses_existing_actions():
    model = _build()
    menus = _submenus_by_label(model)

    assert [a for _l, a in _flatten_items(menus["SSH Pilot"])] == [
        "app.about",
        "app.preferences",
        "app.quit",
    ]
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