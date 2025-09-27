"""Regression tests for terminal pass-through shortcut handling."""

import types

from sshpilot import terminal as terminal_mod


def test_pass_through_mode_allows_ctrl_shift_v(monkeypatch):
    """When pass-through mode is enabled, custom controllers are removed so Ctrl+Shift+V reaches VTE."""

    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)

    removed_controllers = []

    class DummyVte:
        def remove_controller(self, controller):
            removed_controllers.append(controller)

    terminal.vte = DummyVte()
    terminal._shortcut_controller = 'shortcut-controller'
    terminal._scroll_controller = 'scroll-controller'
    terminal._pass_through_mode = False

    monkeypatch.setattr(terminal_mod, 'is_macos', lambda: False)
    monkeypatch.setattr(terminal_cls, '_setup_mouse_wheel_zoom', lambda self: None, raising=False)

    installs = []

    def fake_install(self):
        installs.append('install')
        self._shortcut_controller = 'new-shortcut'

    terminal._install_shortcuts = types.MethodType(fake_install, terminal)

    terminal._apply_pass_through_mode(True)

    assert removed_controllers == ['shortcut-controller', 'scroll-controller']
    assert terminal._shortcut_controller is None
    assert terminal._scroll_controller is None
    assert terminal._pass_through_mode is True
    assert installs == []

    terminal._apply_pass_through_mode(False)

    assert installs == ['install']
    assert terminal._shortcut_controller == 'new-shortcut'
    assert terminal._pass_through_mode is False
