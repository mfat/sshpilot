import importlib
import sys
import types


def _ensure_paramiko_stub():
    if "paramiko" in sys.modules:
        return

    class _DummySSHClient:
        def set_missing_host_key_policy(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            pass

        def open_sftp(self):
            return types.SimpleNamespace(close=lambda: None)

        def close(self):
            pass

    class _DummyPolicy:
        pass

    sys.modules["paramiko"] = types.SimpleNamespace(
        SSHClient=_DummySSHClient,
        AutoAddPolicy=_DummyPolicy,
    )


def _ensure_gi_stub():
    for name in [key for key in sys.modules if key == "gi" or key.startswith("gi.")]:
        del sys.modules[name]

    gi = types.ModuleType("gi")
    gi.require_version = lambda *args, **kwargs: None

    class _DummyModule(types.ModuleType):
        def __getattr__(self, name):
            value = type(name, (), {})
            setattr(self, name, value)
            return value

    repository = _DummyModule("gi.repository")
    gi.repository = repository
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository

    gobject_module = _DummyModule("gi.repository.GObject")
    setattr(gobject_module, "GObject", type("GObject", (), {}))
    setattr(gobject_module, "Object", type("Object", (), {}))
    setattr(
        gobject_module,
        "SignalFlags",
        types.SimpleNamespace(RUN_FIRST=None, RUN_LAST=None),
    )
    repository.GObject = gobject_module
    sys.modules["gi.repository.GObject"] = gobject_module

    glib_module = _DummyModule("gi.repository.GLib")
    setattr(glib_module, "idle_add", lambda *args, **kwargs: None)
    setattr(glib_module, "markup_escape_text", lambda text: text)
    setattr(glib_module, "get_user_config_dir", lambda: "/tmp")
    setattr(glib_module, "get_user_data_dir", lambda: "/tmp")
    setattr(glib_module, "get_home_dir", lambda: "/tmp")
    repository.GLib = glib_module
    sys.modules["gi.repository.GLib"] = glib_module
    platform_utils = sys.modules.get("sshpilot.platform_utils")
    if platform_utils is not None:
        setattr(platform_utils, "GLib", glib_module)

    for name in ["Gtk", "Adw", "Gio", "Gdk", "Pango", "PangoFT2"]:
        module = _DummyModule(f"gi.repository.{name}")
        repository.__dict__[name] = module
        sys.modules[f"gi.repository.{name}"] = module

    class _DummySimpleAction:
        def __init__(self, name=None, parameter_type=None):
            self.name = name
            self.parameter_type = parameter_type
            self.enabled = True
            self._callback = None

        @classmethod
        def new(cls, name, parameter_type):
            return cls(name, parameter_type)

        def connect(self, _signal_name, callback):
            self._callback = callback

        def set_enabled(self, value):
            self.enabled = value

    class _DummySimpleActionGroup:
        def __init__(self):
            self.actions = []

        def add_action(self, action):
            self.actions.append(action)

    class _DummyMenu:
        def __init__(self):
            self.items = []

        def append(self, label, detailed_action):
            self.items.append(("item", label, detailed_action))

        def append_section(self, label, section):
            self.items.append(("section", label, section))

    class _DummyPopoverMenu:
        def __init__(self, model=None):
            self.model = model
            self._parent = None
            self.has_arrow = True
            self.pointing_to = None

        @classmethod
        def new_from_model(cls, model):
            return cls(model)

        def set_has_arrow(self, value):
            self.has_arrow = value

        def insert_action_group(self, _name, _group):
            pass

        def get_parent(self):
            return self._parent

        def set_parent(self, parent):
            self._parent = parent

        def set_pointing_to(self, rect):
            self.pointing_to = rect

        def popup(self):
            pass

    repository.Gio.SimpleAction = _DummySimpleAction
    repository.Gio.SimpleActionGroup = _DummySimpleActionGroup
    repository.Gio.Menu = _DummyMenu
    repository.Gtk.PopoverMenu = _DummyPopoverMenu

    gdk_module = repository.Gdk
    gdk_module.ModifierType = types.SimpleNamespace(
        CONTROL_MASK=1 << 0,
        ALT_MASK=1 << 1,
        SUPER_MASK=1 << 2,
    )
    gdk_module.keyval_to_unicode = lambda value: value


def _load_file_manager_window():
    _ensure_paramiko_stub()
    _ensure_gi_stub()
    module_name = "sshpilot.file_manager_window"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def _make_pane(module, names):
    FilePane = module.FilePane
    FileEntry = module.FileEntry
    pane = FilePane.__new__(FilePane)
    pane._entries = [FileEntry(name, False, 0, 0, None) for name in names]
    return pane


def test_find_prefix_match_basic():
    module = _load_file_manager_window()
    pane = _make_pane(module, ["alpha", "Beta", "gamma", "alphabet"])

    assert pane._find_prefix_match("a", 0) == 0
    assert pane._find_prefix_match("al", 0) == 0
    # Should match case-insensitively and prefer subsequent matches when starting later
    assert pane._find_prefix_match("al", 1) == 3
    # Wrap around to the beginning when the search reaches the end
    assert pane._find_prefix_match("b", 3) == 1


def test_find_prefix_match_no_results():
    module = _load_file_manager_window()
    pane = _make_pane(module, ["alpha", "beta"])
    assert pane._find_prefix_match("z", 0) is None
    assert pane._find_prefix_match("", 0) is None


def test_typeahead_repeated_letter_extends_prefix():
    module = _load_file_manager_window()
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


def test_typeahead_scrolls_list_view_with_full_signature():
    module = _load_file_manager_window()
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
    assert calls == [(2, "flag")]


def test_typeahead_scrolls_grid_view_with_full_signature():
    module = _load_file_manager_window()
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
    assert grid_calls == [(2, "flag")]


def test_context_menu_includes_properties(monkeypatch):
    module = _load_file_manager_window()
    FilePane = module.FilePane
    FileEntry = module.FileEntry

    pane = FilePane.__new__(FilePane)
    pane._is_remote = True
    pane._menu_actions = {}

    class _ActionGroup:
        def __init__(self):
            self.actions = []

        def add_action(self, action):
            self.actions.append(action)

    pane._menu_action_group = _ActionGroup()
    popover = pane._create_menu_model()

    menu_labels = []
    for item_type, label, payload in popover.model.items:
        if item_type == "item":
            menu_labels.append(label)
        elif item_type == "section" and hasattr(payload, "items"):
            for section_item in payload.items:
                if section_item[0] == "item":
                    menu_labels.append(section_item[1])

    assert "Properties…" in menu_labels

    pane._action_buttons = {}
    pane._entries = [FileEntry("example.txt", False, 512, 1700000000, None)]

    class _Selection:
        def is_selected(self, index):
            return index == 0

    pane._selection_model = _Selection()
    pane._current_path = "/tmp"
    pane._update_menu_state()
    assert pane._menu_actions["properties"].enabled is True
