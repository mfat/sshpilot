import types


def _make_pane(module, names):
    FilePane = module.FilePane
    FileEntry = module.FileEntry
    pane = FilePane.__new__(FilePane)
    pane._entries = [FileEntry(name, False, 0, 0, None) for name in names]
    return pane


def test_find_prefix_match_basic(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module, ["alpha", "Beta", "gamma", "alphabet"])

    assert pane._find_prefix_match("a", 0) == 0
    assert pane._find_prefix_match("al", 0) == 0
    # Should match case-insensitively and prefer subsequent matches when starting later
    assert pane._find_prefix_match("al", 1) == 3
    # Wrap around to the beginning when the search reaches the end
    assert pane._find_prefix_match("b", 3) == 1


def test_find_prefix_match_no_results(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module, ["alpha", "beta"])
    assert pane._find_prefix_match("z", 0) is None
    assert pane._find_prefix_match("", 0) is None


def test_typeahead_repeated_letter_extends_prefix(load_file_manager_window):
    module = load_file_manager_window()
    pane = _make_pane(module, ["alpha", "alpine", "ssh", "ssh-agent", "zulu"])

    class _DummySelection:
        def __init__(self):
            self.selected = 0

        def get_selected(self):
            return self.selected

        def set_selected(self, value):
            self.selected = value

    pane._selection_model = _DummySelection()
    pane._scroll_to_position = lambda *args, **kwargs: None
    pane._stack = types.SimpleNamespace(get_visible_child_name=lambda: "list")
    pane._list_view = types.SimpleNamespace(scroll_to=lambda *args, **kwargs: None)
    pane._grid_view = types.SimpleNamespace(scroll_to=lambda *args, **kwargs: None)
    pane._typeahead_buffer = ""
    pane._typeahead_last_time = 0.0

    current_time = 0.0

    def _press(char: str):
        nonlocal current_time
        current_time += 0.1
        pane._current_time = lambda: current_time
        assert pane._on_typeahead_key_pressed(None, ord(char), 0, 0) is True

    _press("s")
    assert pane._selection_model.get_selected() == 2
    assert pane._typeahead_buffer == "s"

    _press("s")
    assert pane._selection_model.get_selected() == 2
    assert pane._typeahead_buffer == "ss"

    _press("h")
    assert pane._selection_model.get_selected() == 2
    assert pane._typeahead_buffer == "ssh"


def test_typeahead_scrolls_list_view_with_full_signature(load_file_manager_window):
    module = load_file_manager_window()
    module.Gtk.ListScrollFlags = types.SimpleNamespace(FOCUS="flag")
    pane = _make_pane(module, ["alpha", "alpine", "zulu"])

    class _DummySelection:
        def __init__(self):
            self.selected = 0

        def get_selected(self):
            return self.selected

        def set_selected(self, value):
            self.selected = value

    calls = []

    def _record(*args):
        calls.append(args)

    pane._selection_model = _DummySelection()
    pane._list_view = types.SimpleNamespace(scroll_to=_record)
    pane._grid_view = types.SimpleNamespace(scroll_to=lambda *args: None)
    pane._stack = types.SimpleNamespace(get_visible_child_name=lambda: "list")
    pane._typeahead_buffer = ""
    pane._typeahead_last_time = 0.0
    pane._current_time = lambda: 0.0

    assert pane._on_typeahead_key_pressed(None, ord("z"), 0, 0) is True
    assert pane._selection_model.get_selected() == 2
    # scroll_to is called with the matched position and a focus flag (the exact
    # flag value is a GtkListScrollFlags detail).
    assert len(calls) == 1
    assert calls[0][0] == 2
    assert len(calls[0]) == 2


def test_typeahead_scrolls_grid_view_with_full_signature(load_file_manager_window):
    module = load_file_manager_window()
    module.Gtk.ListScrollFlags = types.SimpleNamespace(FOCUS="flag")
    pane = _make_pane(module, ["alpha", "alpine", "zulu"])

    class _DummySelection:
        def __init__(self):
            self.selected = 0

        def get_selected(self):
            return self.selected

        def set_selected(self, value):
            self.selected = value

    list_calls = []
    grid_calls = []

    pane._selection_model = _DummySelection()
    pane._list_view = types.SimpleNamespace(scroll_to=lambda *args: list_calls.append(args))
    pane._grid_view = types.SimpleNamespace(scroll_to=lambda *args: grid_calls.append(args))
    pane._stack = types.SimpleNamespace(get_visible_child_name=lambda: "grid")
    pane._typeahead_buffer = ""
    pane._typeahead_last_time = 0.0
    pane._current_time = lambda: 0.0

    assert pane._on_typeahead_key_pressed(None, ord("z"), 0, 0) is True
    assert pane._selection_model.get_selected() == 2
    assert list_calls == []
    assert len(grid_calls) == 1
    assert grid_calls[0][0] == 2
    assert len(grid_calls[0]) == 2


def test_context_menu_includes_properties(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    FilePane = module.FilePane
    FileEntry = module.FileEntry

    pane = FilePane.__new__(FilePane)
    pane._is_remote = True
    pane._menu_actions = {}
    pane._menu_action_callbacks = {}

    class _ActionGroup:
        def __init__(self):
            self.actions = []

        def add_action(self, action):
            self.actions.append(action)

    pane._menu_action_group = _ActionGroup()
    # The context menu is now a Gtk.Popover whose rows are built dynamically in
    # _show_context_menu; the selectable operations are registered as Gio actions
    # in _menu_actions. Verify the Properties action exists alongside the core ops.
    pane._create_menu_model()

    assert "properties" in pane._menu_actions
    for expected in ("download", "upload", "edit", "copy", "cut", "paste", "rename", "delete"):
        assert expected in pane._menu_actions

    pane._action_buttons = {}
    pane._entries = [FileEntry("example.txt", False, 512, 1700000000, None)]

    class _Selection:
        def is_selected(self, index):
            return index == 0

    pane._selection_model = _Selection()
    pane._current_path = "/tmp"
    pane._update_menu_state()
    assert pane._menu_actions["properties"].enabled is True


def test_multi_selection_keeps_properties_and_hides_rename(load_file_manager_window):
    module = load_file_manager_window()
    FilePane = module.FilePane
    FileEntry = module.FileEntry

    pane = FilePane.__new__(FilePane)
    pane._is_remote = True
    pane._menu_actions = {}
    pane._menu_action_callbacks = {}

    class _ActionGroup:
        def __init__(self):
            self.actions = []

        def add_action(self, action):
            self.actions.append(action)

    pane._menu_action_group = _ActionGroup()
    pane._create_menu_model()
    pane._action_buttons = {}
    pane._entries = [
        FileEntry("a.txt", False, 1, 0.0, None),
        FileEntry("b.txt", False, 2, 0.0, None),
    ]
    pane._selection_model = type(
        "Selection",
        (),
        {"is_selected": staticmethod(lambda index: index in {0, 1})},
    )()
    pane._current_path = "/tmp"
    pane._can_paste = False
    pane._update_menu_state()

    assert pane._menu_actions["properties"].enabled is True
    assert pane._menu_actions["rename"].enabled is False
    assert pane._menu_actions["delete"].enabled is True
    assert pane._menu_actions["edit"].enabled is False
