"""Selection behaviour that mirrors GNOME Nautilus.

Nautilus clears the selection on a plain background click, keeps it on a
background right-click, narrows a multi-selection to the clicked item on
release, keeps the selection across a reload, selects the folder the user came
out of when moving up, and inverts the selection with Ctrl+Shift+I.
"""

import sys
import types

import pytest

SHIFT = 1 << 3
CTRL = 1 << 0
BUTTON_PRIMARY = 1
BUTTON_SECONDARY = 3


class FakeSelectionModel:
    def __init__(self, selected=()):
        self.selected = set(selected)

    def is_selected(self, index):
        return index in self.selected

    def unselect_all(self):
        self.selected = set()

    def select_item(self, index, unselect_rest):
        if unselect_rest:
            self.selected = set()
        self.selected.add(index)

    def unselect_item(self, index):
        self.selected.discard(index)


class FakeGesture:
    def __init__(self, state=0, button=BUTTON_PRIMARY):
        self._state = state
        self._button = button

    def get_current_event_state(self):
        return self._state

    def get_current_button(self):
        return self._button


class FakeWidget:
    def __init__(self, css_name="widget", parent=None, **attrs):
        self._css_name = css_name
        self._parent = parent
        self.focused = False
        for key, value in attrs.items():
            setattr(self, key, value)

    def get_css_name(self):
        return self._css_name

    def get_parent(self):
        return self._parent

    def grab_focus(self):
        self.focused = True


@pytest.fixture
def pane_module(load_file_manager_window, monkeypatch):
    load_file_manager_window()
    module = sys.modules["sshpilot.file_manager.pane"]
    monkeypatch.setattr(
        module.Gdk,
        "ModifierType",
        types.SimpleNamespace(CONTROL_MASK=CTRL, ALT_MASK=1 << 1, SUPER_MASK=1 << 2, SHIFT_MASK=SHIFT),
        raising=False,
    )
    monkeypatch.setattr(module.Gdk, "BUTTON_SECONDARY", BUTTON_SECONDARY, raising=False)
    monkeypatch.setattr(module, "is_macos", lambda: False)
    return module


def _make_pane(module, names=("a", "b", "c", "d"), selected=()):
    pane = module.FilePane.__new__(module.FilePane)
    pane._entries = [module.FileEntry(name, False, 0, 0.0, None) for name in names]
    pane._selection_model = FakeSelectionModel(selected)
    pane._selection_anchor = None
    pane._pending_grid_collapse = None
    pane._anchor_follows_focus = False
    pane._update_menu_state = lambda: None
    return pane


# -- empty-space detection ---------------------------------------------------


def _pane_with_view(module, picked_factory):
    pane = _make_pane(module)
    view = FakeWidget("columnview")
    view.translate_coordinates = lambda _target, x, y: (x, y)
    picked = picked_factory(view)
    view.pick = lambda _x, _y, _flags: picked
    pane._list_view = view
    pane._grid_view = object()
    pane._stack = types.SimpleNamespace(get_visible_child=lambda: view)
    module.Gtk.ScrolledWindow = type("ScrolledWindow", (), {})
    module.Gtk.PickFlags = types.SimpleNamespace(DEFAULT=0)
    return pane, view


def test_click_below_the_rows_is_background(pane_module):
    pane, view = _pane_with_view(pane_module, lambda view: FakeWidget("listview", parent=view))
    assert pane._is_click_on_empty_space(view, 5, 500) is True


def test_click_on_a_row_padding_is_not_background(pane_module):
    def picked(view):
        row = FakeWidget("row", parent=FakeWidget("listview", parent=view))
        return FakeWidget("box", parent=row)

    pane, view = _pane_with_view(pane_module, picked)
    assert pane._is_click_on_empty_space(view, 5, 5) is False


def test_click_on_the_column_header_is_not_background(pane_module):
    def picked(view):
        header = FakeWidget("header", parent=view)
        return FakeWidget("button", parent=header)

    pane, view = _pane_with_view(pane_module, picked)
    assert pane._is_click_on_empty_space(view, 5, 5) is False


# -- background clicks -------------------------------------------------------


def test_plain_background_click_clears_the_selection(pane_module):
    pane = _make_pane(pane_module, selected={0, 2})
    pane._selection_anchor = 2
    pane._is_click_on_empty_space = lambda *_: True
    widget = FakeWidget()

    pane._on_view_background_pressed(FakeGesture(), 1, 0, 0, widget)

    assert pane._selection_model.selected == set()
    assert pane._selection_anchor is None
    assert widget.focused


@pytest.mark.parametrize("state", [CTRL, SHIFT])
def test_modified_background_click_keeps_the_selection(pane_module, state):
    pane = _make_pane(pane_module, selected={0, 2})
    pane._is_click_on_empty_space = lambda *_: True

    pane._on_view_background_pressed(FakeGesture(state=state), 1, 0, 0, FakeWidget())

    assert pane._selection_model.selected == {0, 2}


def test_background_right_click_does_not_clear(pane_module):
    pane = _make_pane(pane_module, selected={1})
    pane._is_click_on_empty_space = lambda *_: True

    pane._on_view_background_pressed(
        FakeGesture(button=BUTTON_SECONDARY), 1, 0, 0, FakeWidget()
    )

    assert pane._selection_model.selected == {1}


def test_click_on_an_item_is_left_to_the_item(pane_module):
    pane = _make_pane(pane_module, selected={1})
    pane._is_click_on_empty_space = lambda *_: False

    pane._on_view_background_pressed(FakeGesture(), 1, 0, 0, FakeWidget())

    assert pane._selection_model.selected == {1}


def test_background_menu_shows_folder_properties(pane_module):
    pane = _make_pane(pane_module, selected={1})
    pane._menu_for_background = True
    pane._current_path = "/srv"
    pane._is_remote = True
    seen = []
    pane.get_selected_entries = lambda: seen.append("selection") or [pane._entries[1]]
    presented = []
    pane._show_properties_dialog = (
        lambda entries, details, properties_path=None: presented.append(
            (list(entries) if not isinstance(entries, pane_module.FileEntry) else [entries], properties_path)
        )
    )
    pane._on_menu_properties()
    assert seen == []
    assert len(presented) == 1
    assert presented[0][0][0].name == "srv"
    assert presented[0][1] == "/"


def test_properties_uses_the_full_multi_selection(pane_module):
    pane = _make_pane(pane_module, names=("a.txt", "b.txt", "c.txt"), selected={0, 2})
    pane._menu_for_background = False
    pane._current_path = "/srv"
    pane._is_remote = True
    presented = []
    pane._show_properties_dialog = (
        lambda entries, details, properties_path=None: presented.append(
            [e.name for e in entries]
        )
    )
    pane._on_menu_properties()
    assert presented == [["a.txt", "c.txt"]]


# -- grid clicks -------------------------------------------------------------


def test_plain_click_on_a_selected_item_narrows_on_release(pane_module):
    pane = _make_pane(pane_module, selected={0, 1, 2})
    button = types.SimpleNamespace(drag_position=1)

    pane._update_grid_selection_for_press(1, FakeGesture())
    # Still the whole group while the button is down, so it can be dragged.
    assert pane._selection_model.selected == {0, 1, 2}

    pane._on_grid_cell_released(FakeGesture(), 1, 0, 0, button)
    assert pane._selection_model.selected == {1}
    assert pane._selection_anchor == 1


def test_drag_keeps_the_group(pane_module):
    pane = _make_pane(pane_module, selected={0, 1, 2})
    button = types.SimpleNamespace(drag_position=1)

    pane._update_grid_selection_for_press(1, FakeGesture())
    pane._on_grid_cell_stopped(FakeGesture())
    pane._on_grid_cell_released(FakeGesture(), 1, 0, 0, button)

    assert pane._selection_model.selected == {0, 1, 2}


# -- list item press vs rubberband (GTK #5670 / Nautilus) ---------------------


def test_list_item_press_disables_rubberband_for_dnd(pane_module):
    pane = _make_pane(pane_module)
    calls = []
    pane._list_view = types.SimpleNamespace(
        set_enable_rubberband=lambda enabled: calls.append(enabled)
    )

    pane._on_list_item_pressed(FakeGesture(), 1, 0, 0)
    assert calls == [False]

    pane._on_list_item_released(FakeGesture(), 1, 0, 0)
    assert calls == [False, True]


def test_list_item_stop_re_enables_rubberband(pane_module):
    pane = _make_pane(pane_module)
    calls = []
    pane._list_view = types.SimpleNamespace(
        set_enable_rubberband=lambda enabled: calls.append(enabled)
    )

    pane._on_list_item_pressed(FakeGesture(), 1, 0, 0)
    pane._on_list_item_stopped(FakeGesture())
    assert calls == [False, True]


def test_grid_item_press_disables_rubberband_for_dnd(pane_module):
    pane = _make_pane(pane_module, selected={0})
    calls = []
    pane._grid_view = types.SimpleNamespace(
        set_enable_rubberband=lambda enabled: calls.append(enabled)
    )
    button = types.SimpleNamespace(drag_position=0)

    pane._on_grid_cell_pressed(FakeGesture(), 1, 0, 0, button)
    assert calls == [False]

    pane._on_grid_cell_released(FakeGesture(), 1, 0, 0, button)
    assert calls == [False, True]


def test_grid_item_stop_re_enables_rubberband(pane_module):
    pane = _make_pane(pane_module, selected={0, 1})
    calls = []
    pane._grid_view = types.SimpleNamespace(
        set_enable_rubberband=lambda enabled: calls.append(enabled)
    )
    button = types.SimpleNamespace(drag_position=1)

    pane._on_grid_cell_pressed(FakeGesture(), 1, 0, 0, button)
    pane._on_grid_cell_stopped(FakeGesture())
    assert calls == [False, True]


def test_shift_click_extends_from_keyboard_focus(pane_module):
    pane = _make_pane(pane_module, selected={0})
    pane._selection_anchor = 0
    focus_button = FakeWidget("button", drag_position=2)
    focus_child = FakeWidget("child")
    focus_child.get_first_child = lambda: focus_button
    pane._grid_view = types.SimpleNamespace(get_focus_child=lambda: focus_child)
    pane._GRID_NAV_KEYS = frozenset({"right"})

    # A plain arrow moved focus (and GTK's selection) to item 2.
    pane._on_grid_nav_key_pressed(None, "right", 0, 0)
    pane._selection_model.select_item(2, True)

    pane._update_grid_selection_for_press(3, FakeGesture(state=SHIFT))

    assert pane._selection_model.selected == {2, 3}


# -- reload and navigation ---------------------------------------------------


def _loaded_pane(module, monkeypatch, path, names):
    pane = _make_pane(module, names=())
    pane._current_path = path
    pane._cached_entries = []
    pane._raw_entries = []
    pane._show_hidden = False
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._is_remote = False
    pane._clear_load_error = lambda: None
    pane._set_current_pathbar_text = lambda _p: None
    pane._scroll_to_position = lambda _p: None
    store = types.SimpleNamespace(items=[])
    store.get_n_items = lambda: len(store.items)

    def splice(pos, n, additions):
        store.items[pos:pos + n] = list(additions)

    store.splice = splice
    pane._list_store = store
    monkeypatch.setattr(
        module.Gtk,
        "StringObject",
        types.SimpleNamespace(new=lambda value: value),
        raising=False,
    )
    pane.show_entries(path, [module.FileEntry(n, True, 0, 0.0, None) for n in names])
    return pane


def _dirs(module, names):
    return [module.FileEntry(n, True, 0, 0.0, None) for n in names]


def test_reload_keeps_the_selection(pane_module, monkeypatch):
    pane = _loaded_pane(pane_module, monkeypatch, "/srv", ["a", "b", "c"])
    pane._selection_model.select_item(1, False)

    pane.show_entries("/srv/", _dirs(pane_module, ["a", "b", "c", "new"]))

    assert [pane._entries[i].name for i in pane._selection_model.selected] == ["b"]


def test_entering_a_folder_starts_unselected(pane_module, monkeypatch):
    pane = _loaded_pane(pane_module, monkeypatch, "/srv", ["a", "b"])
    pane._selection_model.select_item(1, False)

    pane.show_entries("/srv/b", _dirs(pane_module, ["x", "y"]))

    assert pane._selection_model.selected == set()


def test_going_up_selects_the_folder_just_left(pane_module, monkeypatch):
    pane = _loaded_pane(pane_module, monkeypatch, "/srv/b/deep", ["x"])

    pane.show_entries("/srv", _dirs(pane_module, ["a", "b", "c"]))

    assert [pane._entries[i].name for i in pane._selection_model.selected] == ["b"]


def test_child_toward(pane_module):
    child_toward = pane_module._child_toward
    assert child_toward("/", "/home/user") == "home"
    assert child_toward("/home", "/home/user/docs") == "user"
    assert child_toward("/home/", "/home/user") == "user"
    assert child_toward("/home", "/homework") is None
    assert child_toward("/home", "/home") is None
    assert child_toward("/home/user", "/home") is None


# -- invert ------------------------------------------------------------------


def test_invert_selection(pane_module):
    pane = _make_pane(pane_module, selected={0, 2})

    assert pane._shortcut_invert_selection() is True

    assert pane._selection_model.selected == {1, 3}
