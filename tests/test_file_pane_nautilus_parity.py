"""Nautilus behaviours of the file pane: sorting, history, shortcuts, rename,
Select Pattern and Copy Location."""

import sys
import types

import pytest


def _entries(module, names, dirs=()):
    FileEntry = module.FileEntry
    return [FileEntry(name, name in dirs, 0, 0, None) for name in names]


class _Selection:
    def __init__(self):
        self.selected = set()

    def unselect_all(self):
        self.selected.clear()

    def select_item(self, index, _exclusive):
        self.selected.add(index)

    def is_selected(self, index):
        return index in self.selected


def _make_pane(module, names=(), dirs=()):
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane._entries = _entries(module, names, dirs)
    pane._selection_model = _Selection()
    pane._selection_anchor = None
    pane._scroll_to_position = lambda *_args: None
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._history = []
    pane._forward_history = []
    pane._suppress_history_push = False
    pane._current_path = "/srv"
    pane._is_remote = True
    pane._menu_for_background = False
    pane.toolbar = types.SimpleNamespace(controls=None)
    return pane


def test_name_sort_is_natural(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module)
    names = ["file10.txt", "File2.txt", "file1.txt", "a", "file02b"]
    ordered = [e.name for e in pane._sort_entries(_entries(module, names))]
    assert ordered == ["a", "file1.txt", "File2.txt", "file02b", "file10.txt"]


def test_folders_stay_first_and_ties_fall_back_to_name(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module)
    pane._sort_key = "size"
    entries = _entries(module, ["b10", "b9", "dir"], dirs={"dir"})
    assert [e.name for e in pane._sort_entries(entries)] == ["dir", "b9", "b10"]


def test_sort_by_type_groups_files_by_type(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module)
    pane._sort_key = "type"
    # The test gi stub has no content types, so types fall back to extensions.
    entries = _entries(module, ["b.txt", "a.py", "c.py", "a.txt"])
    assert [e.name for e in pane._sort_entries(entries)] == ["a.py", "c.py", "a.txt", "b.txt"]


@pytest.mark.parametrize(
    "name, offset",
    [
        ("report.pdf", 6),
        ("archive.tar.gz", 7),
        (".bashrc", 7),
        ("no_extension", 12),
        ("trailing.", 9),
        ("my file.has space", 17),
        ("a.b.c", 3),
        ("", 0),
    ],
)
def test_rename_selects_name_without_extension(load_file_manager_window, name, offset):
    load_file_manager_window()
    from sshpilot.file_manager.format_utils import filename_extension_offset

    assert filename_extension_offset(name) == offset


def test_back_and_forward_walk_history(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module)
    visited = []
    pane.emit = lambda _signal, path: visited.append(path)

    for path in ("/a", "/b", "/c"):
        pane.push_history(path)

    pane._on_back_clicked(None)
    pane._on_back_clicked(None)
    assert visited == ["/b", "/a"]
    assert pane._forward_history == ["/c", "/b"]

    pane._on_forward_clicked(None)
    assert visited[-1] == "/b"
    assert pane._history == ["/a", "/b"]
    assert pane._suppress_history_push is True

    # Going somewhere new drops what was ahead.
    pane.push_history("/d")
    assert pane._forward_history == []
    pane._on_forward_clicked(None)
    assert visited[-1] == "/b"


def test_select_pattern_matches_like_nautilus(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module, ["a.txt", "B.TXT", "notes.md", "a.txt.bak", "[x].txt"])

    assert pane.select_pattern("*.txt") == 2
    assert pane._selection_model.selected == {0, 4}
    assert pane.select_pattern("?.txt") == 1
    assert pane._selection_model.selected == {0}
    assert pane.select_pattern("zzz") == 0
    assert pane._selection_model.selected == set()


def test_copy_location_paths(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module, ["one", "two"])
    assert pane._selection_locations() == ["/srv"]

    pane._selection_model.selected = {0, 1}
    assert pane._selection_locations() == ["/srv/one", "/srv/two"]

    pane._menu_for_background = True
    assert pane._selection_locations() == ["/srv"]


def test_alt_down_opens_the_selected_folder(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module, ["docs", "file"], dirs={"docs"})
    opened = []
    pane.emit = lambda _signal, path: opened.append(path)

    pane._selection_model.selected = {1}
    pane._shortcut_navigate("down")
    assert opened == []

    pane._selection_model.selected = {0}
    pane._shortcut_navigate("down")
    assert opened == ["/srv/docs"]


def _registered_triggers(module, monkeypatch, *, mac):
    parsed = []

    class _Controller:
        def set_scope(self, _scope):
            pass

        def add_shortcut(self, _shortcut):
            pass

    gtk = module.Gtk
    monkeypatch.setattr(gtk, "ShortcutController", _Controller, raising=False)
    monkeypatch.setattr(
        gtk,
        "ShortcutTrigger",
        types.SimpleNamespace(parse_string=lambda s: parsed.append(s) or s),
        raising=False,
    )
    monkeypatch.setattr(
        gtk, "KeyvalTrigger", types.SimpleNamespace(new=lambda *_a: object()), raising=False
    )
    monkeypatch.setattr(
        gtk, "CallbackAction", types.SimpleNamespace(new=lambda _cb: object()), raising=False
    )
    monkeypatch.setattr(
        gtk, "Shortcut", types.SimpleNamespace(new=lambda *_a: object()), raising=False
    )
    monkeypatch.setattr(gtk, "ShortcutScope", types.SimpleNamespace(LOCAL=0), raising=False)
    monkeypatch.setattr(gtk, "TextDirection", types.SimpleNamespace(RTL=2), raising=False)
    monkeypatch.setattr(
        gtk, "Widget", types.SimpleNamespace(get_default_direction=lambda: 1), raising=False
    )
    pane_module = sys.modules["sshpilot.file_manager.pane"]

    class _Mods(int):
        SHIFT_MASK = 1

    monkeypatch.setattr(pane_module.Gdk, "ModifierType", _Mods, raising=False)
    monkeypatch.setattr(pane_module, "is_macos", lambda: mac)

    pane = _make_pane(module)
    view = types.SimpleNamespace(add_controller=lambda _c: None)
    pane._attach_shortcuts(view)
    return parsed


def test_linux_uses_nautilus_keys(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    triggers = _registered_triggers(module, monkeypatch, mac=False)
    assert "<primary>h" in triggers
    assert "<alt>Left|Back" in triggers
    assert "<alt>Up" in triggers
    assert "<shift><primary>n" in triggers
    assert "<primary>i|<alt>Return" in triggers
    assert "Menu|<shift>F10" in triggers
    assert "<primary>s" in triggers
    assert "<primary>BackSpace" not in triggers


def test_macos_uses_finder_keys(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    triggers = _registered_triggers(module, monkeypatch, mac=True)
    # Cmd+H hides the app on macOS; Finder toggles hidden files with Cmd+Shift+.
    assert "<primary>h" not in triggers
    assert "<shift><primary>period|<shift><primary>greater" in triggers
    assert "<primary>bracketleft|Back" in triggers
    assert "<primary>bracketright|Forward" in triggers
    assert "<primary>Up" in triggers
    assert "<primary>BackSpace" in triggers


def test_list_and_grid_zoom_separately(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    pane = _make_pane(module)
    pane._icon_levels = {"list": 1, "grid": 2}
    pane._bound_list_icons = set()
    pane._bound_grid_images = set()
    persisted = []
    pane._persist_icon_level = persisted.append
    visible = {"name": "grid"}
    pane._stack = types.SimpleNamespace(get_visible_child_name=lambda: visible["name"])

    pane._request_zoom(1)
    pane._request_zoom(1)
    pane._request_zoom(1)  # past the largest grid step: stays at 4
    assert pane._icon_levels == {"list": 1, "grid": 4}
    assert pane._grid_icon_px() == 256

    visible["name"] = "list"
    pane._shortcut_zoom(-1)
    assert pane._icon_levels == {"list": 0, "grid": 4}
    assert pane._list_icon_px() == 16
    pane._shortcut_zoom(0)
    assert pane._icon_levels == {"list": 1, "grid": 4}
    assert persisted == ["grid", "grid", "list", "list"]


def test_legacy_single_zoom_setting_migrates_per_view():
    from sshpilot.core.settings.migration import ensure_config_defaults

    config, updated = ensure_config_defaults({"file_manager": {"icon_size_level": 4}})
    fm = config["file_manager"]
    assert updated
    assert "icon_size_level" not in fm
    assert (fm["list_icon_level"], fm["grid_icon_level"]) == (2, 3)

    config, _ = ensure_config_defaults({"file_manager": {"grid_icon_level": 9}})
    assert config["file_manager"]["grid_icon_level"] == 4
    assert config["file_manager"]["list_icon_level"] == 1
