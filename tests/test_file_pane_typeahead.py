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
    repository.GLib = glib_module
    sys.modules["gi.repository.GLib"] = glib_module

    for name in ["Gtk", "Adw", "Gio", "Gdk", "Pango", "PangoFT2"]:
        module = _DummyModule(f"gi.repository.{name}")
        repository.__dict__[name] = module
        sys.modules[f"gi.repository.{name}"] = module

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
    pane._entries = [FileEntry(name, False, 0, 0) for name in names]
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
