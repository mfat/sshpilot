"""Regression tests for terminal pass-through shortcut handling."""

from sshpilot import terminal as terminal_mod


def test_pass_through_mode_toggles_accelerators(monkeypatch):
    """Pass-through mode should disable accelerators and re-enable them when turned off."""

    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)

    class DummyManager:
        def __init__(self):
            self.calls = []

        def set_accels_for_action(self, action, accels):
            self.calls.append((action, tuple(accels)))

    manager = DummyManager()

    terminal.vte = object()
    terminal._scroll_controller = 'scroll-controller'
    terminal._pass_through_mode = False

    monkeypatch.setattr(terminal_mod, 'is_macos', lambda: False)
    monkeypatch.setattr(terminal_cls, '_setup_mouse_wheel_zoom', lambda self: None, raising=False)
    monkeypatch.setattr(terminal_cls, '_remove_mouse_wheel_zoom', lambda self: None, raising=False)
    monkeypatch.setattr(terminal_cls, '_get_accel_manager', lambda self: manager)

    terminal._apply_pass_through_mode(True)

    expected_disabled = [
        ('term.copy', ()),
        ('term.paste', ()),
        ('term.select_all', ()),
        ('term.zoom_in', ()),
        ('term.zoom_out', ()),
        ('term.reset_zoom', ()),
    ]
    assert manager.calls[:6] == expected_disabled
    assert terminal._pass_through_mode is True

    terminal._apply_pass_through_mode(False)

    expected_enabled = [
        ('term.copy', ('<Primary><Shift>c',)),
        ('term.paste', ('<Primary><Shift>v',)),
        ('term.select_all', ('<Primary><Shift>a',)),
        ('term.zoom_in', ('<Primary>equal', '<Primary>KP_Add')),
        ('term.zoom_out', ('<Primary>minus', '<Primary>KP_Subtract')),
        ('term.reset_zoom', ('<Primary>0',)),
    ]
    assert manager.calls[6:] == expected_enabled
    assert terminal._pass_through_mode is False
