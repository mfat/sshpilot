"""The gi and paramiko stubs the file pane tests import FilePane against.

Installed only through conftest's ``load_file_manager_window`` fixture, which
also undoes them. Calling this directly leaves the stubs in sys.modules for
every later test on the worker.
"""

import sys
import types


def _install_paramiko_stub():
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


def _install_gi_stub(monkeypatch):
    for name in [key for key in sys.modules if key == "gi" or key.startswith("gi.")]:
        del sys.modules[name]

    gi = types.ModuleType("gi")
    gi.require_version = lambda *args, **kwargs: None

    class _DummyModule(types.ModuleType):
        def __getattr__(self, name):
            # Module introspection probes dunders such as ``__file__``.
            # Answering those with a dummy class made Hypothesis, which walks
            # sys.modules for local source files, crash with "argument of type
            # 'type' is not iterable" (conftest's gi stub guards the same way).
            if name.startswith("__"):
                raise AttributeError(name)
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
    # platform_utils outlives the registry restore, so rebind its GLib through
    # monkeypatch or later tests would keep calling this stub.
    platform_utils = sys.modules.get("sshpilot.platform_utils")
    if platform_utils is not None:
        monkeypatch.setattr(platform_utils, "GLib", glib_module, raising=False)

    for name in ["Gtk", "Adw", "Gio", "Gdk", "Pango", "PangoFT2"]:
        module = _DummyModule(f"gi.repository.{name}")
        repository.__dict__[name] = module
        sys.modules[f"gi.repository.{name}"] = module

    # Blueprint-templated widgets use @Gtk.Template / Gtk.Template.Child /
    # @Gtk.Template.Callback; make them no-ops under this stub so the classes
    # import (the auto-generated dummy class would reject the decorator args).
    from gtk_template_stub import install_template_stub
    install_template_stub(sys.modules["gi.repository.Gtk"])

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

    class _DummyPopover:
        def __init__(self):
            self.child = None
            self.has_arrow = True

        @classmethod
        def new(cls):
            return cls()

        def set_has_arrow(self, value):
            self.has_arrow = value

        def set_child(self, child):
            self.child = child

    class _DummyListBox:
        def __init__(self, **kwargs):
            self.rows = []

        def set_selection_mode(self, mode):
            self._mode = mode

    repository.Gio.SimpleAction = _DummySimpleAction
    repository.Gio.SimpleActionGroup = _DummySimpleActionGroup
    repository.Gio.Menu = _DummyMenu
    repository.Gtk.PopoverMenu = _DummyPopoverMenu
    repository.Gtk.Popover = _DummyPopover
    repository.Gtk.ListBox = _DummyListBox
    repository.Gtk.SelectionMode = types.SimpleNamespace(NONE=0)

    gdk_module = repository.Gdk
    gdk_module.ModifierType = types.SimpleNamespace(
        CONTROL_MASK=1 << 0,
        ALT_MASK=1 << 1,
        SUPER_MASK=1 << 2,
    )
    gdk_module.keyval_to_unicode = lambda value: value


def install_file_pane_stubs(monkeypatch):
    _install_paramiko_stub()
    _install_gi_stub(monkeypatch)
