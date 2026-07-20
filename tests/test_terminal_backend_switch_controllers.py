"""Regression tests for controller teardown across a terminal backend switch.

Custom controllers attach to `vte` when the VTE backend is active and to
`terminal_widget` under PyXterm. Before this was fixed, teardown always targeted
`vte`, and `ensure_backend()` never tore down at all -- so after a VTE<->PyXterm
switch the copy/paste/zoom and Ctrl+F controllers stayed on the destroyed widget
and were never reinstalled.
"""

import sys
import types

gi = types.ModuleType("gi")
gi.require_version = lambda *args, **kwargs: None
repository = types.SimpleNamespace()
repository.Gtk = types.SimpleNamespace(Box=type("Box", (), {}))
repository.GObject = types.SimpleNamespace(
    SignalFlags=types.SimpleNamespace(RUN_FIRST=0)
)
repository.GLib = types.SimpleNamespace(idle_add=lambda *a, **k: None)
repository.Vte = types.SimpleNamespace()
repository.Pango = types.SimpleNamespace()
repository.Gdk = types.SimpleNamespace()
repository.Gio = types.SimpleNamespace()
repository.Adw = types.SimpleNamespace(Toast=types.SimpleNamespace(new=lambda *a, **k: None))
gi.repository = repository
sys.modules.setdefault("gi", gi)
sys.modules.setdefault("gi.repository", repository)
for name in ["Gtk", "GObject", "GLib", "Vte", "Pango", "Gdk", "Gio", "Adw"]:
    sys.modules.setdefault(f"gi.repository.{name}", getattr(repository, name))

from sshpilot import terminal as terminal_mod


class DummyWidget:
    """Widget that records the controllers removed from it."""

    def __init__(self, name):
        self.name = name
        self.removed = []

    def remove_controller(self, controller):
        self.removed.append(controller)

    # ensure_backend() pokes these on the new widget
    def set_hexpand(self, _value):
        pass

    def set_vexpand(self, _value):
        pass

    def set_visible(self, _value):
        pass


def _bare_terminal():
    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)
    terminal._pass_through_mode = False
    terminal._shortcut_controller = 'shortcut-controller'
    terminal._scroll_controller = 'scroll-controller'
    terminal._search = None
    return terminal


def test_teardown_targets_terminal_widget_when_no_vte():
    """PyXterm has no `vte`; the controllers must come off `terminal_widget`."""
    terminal = _bare_terminal()
    terminal.vte = None
    terminal.terminal_widget = DummyWidget('pyxterm')

    assert terminal.controller_host() is terminal.terminal_widget

    terminal._remove_custom_shortcut_controllers()

    assert terminal.terminal_widget.removed == ['shortcut-controller', 'scroll-controller']
    assert terminal._shortcut_controller is None
    assert terminal._scroll_controller is None


def test_backend_switch_detaches_from_old_widget_and_reinstalls(monkeypatch):
    terminal_cls = terminal_mod.TerminalWidget
    terminal = _bare_terminal()

    old_widget = DummyWidget('old-vte')
    new_widget = DummyWidget('new-pyxterm')
    terminal.vte = old_widget
    terminal.terminal_widget = old_widget
    terminal.config = None

    class DummyBackend:
        def __init__(self):
            self.vte = None
            self.widget = new_widget
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    old_backend = types.SimpleNamespace(destroy=lambda: None)
    new_backend = DummyBackend()
    terminal.backend = old_backend

    class DummyScrolled:
        def __init__(self):
            self.child = old_widget

        def set_child(self, child):
            self.child = child

    terminal.scrolled_window = DummyScrolled()
    terminal._menu_popover = None
    terminal._menu_gesture = None

    monkeypatch.setattr(terminal_cls, 'get_backend_name', lambda self: 'vte', raising=False)
    monkeypatch.setattr(terminal_cls, '_disconnect_backend_signals', lambda self: None, raising=False)
    monkeypatch.setattr(terminal_cls, '_connect_backend_signals', lambda self: None, raising=False)
    monkeypatch.setattr(terminal_cls, '_create_backend', lambda self, name=None: new_backend, raising=False)
    monkeypatch.setattr(terminal_cls, 'apply_theme', lambda self: None, raising=False)

    # The real setup_terminal() ends in _apply_pass_through_mode(), which is the
    # hook that reinstalls the shortcuts once teardown has cleared them.
    def fake_setup_terminal(self):
        self._apply_pass_through_mode(self._pass_through_mode)

    monkeypatch.setattr(terminal_cls, 'setup_terminal', fake_setup_terminal, raising=False)

    installed_on = []

    def fake_install(self):
        installed_on.append(self.controller_host())
        self._shortcut_controller = 'new-shortcut'

    monkeypatch.setattr(terminal_cls, '_install_shortcuts', fake_install, raising=False)

    terminal.ensure_backend('pyxterm')

    # Detached from the widget the controllers were actually on...
    assert old_widget.removed == ['shortcut-controller', 'scroll-controller']
    assert new_widget.removed == []
    # ...and reinstalled on the new one.
    assert installed_on == [new_widget]
    assert terminal._shortcut_controller == 'new-shortcut'
