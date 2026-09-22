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

    terminal.terminal_widget = DummyVte()
    terminal.terminal_container = DummyVte()
    terminal._shortcut_controller = 'shortcut-controller'
    terminal._zoom_controller = 'zoom-controller'
    terminal._scroll_controller = 'scroll-controller'
    terminal._pass_through_mode = False
    backend_modes = []
    terminal.backend = types.SimpleNamespace(
        set_shortcut_passthrough=backend_modes.append)

    monkeypatch.setattr(terminal_mod, 'is_macos', lambda: False)
    monkeypatch.setattr(terminal_cls, '_setup_scroll_controllers', lambda self: None, raising=False)

    installs = []

    def fake_install(self):
        installs.append('install')
        self._shortcut_controller = 'new-shortcut'

    terminal._install_shortcuts = types.MethodType(fake_install, terminal)

    terminal._apply_pass_through_mode(True)

    assert removed_controllers == ['shortcut-controller', 'zoom-controller']
    assert terminal._shortcut_controller is None
    assert terminal._zoom_controller is None
    # The wheel keeps scrolling the scrollback in pass-through mode.
    assert terminal._scroll_controller == 'scroll-controller'
    assert terminal._pass_through_mode is True
    assert backend_modes == [True]
    assert installs == []

    terminal._apply_pass_through_mode(False)

    assert installs == ['install']
    assert terminal._shortcut_controller == 'new-shortcut'
    assert terminal._pass_through_mode is False
    assert backend_modes == [True, False]


# Removed: test_prepare_key_native_mode_falls_back — TerminalWidget._prepare_key_for_native_mode
# and _resolve_native_identity_candidates were removed; native key preload is handled by the
# keyring/askpass auth path (ssh_connection_builder + askpass_utils).
