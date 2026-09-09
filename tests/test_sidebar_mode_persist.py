"""Icon-strip sidebar mode is retired; collapse requests are ignored."""

from types import SimpleNamespace

from sshpilot.window import MainWindow


class _Config:
    def __init__(self, mode='full'):
        self.settings = {'ui.sidebar_mode': mode}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def _window(mode='full', minimal=False):
    win = SimpleNamespace(
        config=_Config(mode),
        _sidebar_minimal=minimal,
        applied=[],
    )
    win._persist_sidebar_mode = MainWindow._persist_sidebar_mode.__get__(win)
    win._on_sidebar_drag_mode_switch = (
        MainWindow._on_sidebar_drag_mode_switch.__get__(win))
    win.set_sidebar_minimal = (
        lambda minimal, animate=True: win.applied.append((minimal, animate)))
    return win


def test_drag_to_minimal_is_ignored():
    win = _window(mode='full', minimal=False)
    win._on_sidebar_drag_mode_switch(True)
    assert win.config.settings['ui.sidebar_mode'] == 'full'
    assert win.applied == []


def test_drag_to_full_clears_leftover_strip():
    win = _window(mode='minimal', minimal=True)
    win._on_sidebar_drag_mode_switch(False)
    assert win.config.settings['ui.sidebar_mode'] == 'full'
    assert win.applied == [(False, False)]


def test_persist_sidebar_mode_always_full():
    win = _window(mode='minimal')
    win._persist_sidebar_mode(True)
    assert win.config.settings['ui.sidebar_mode'] == 'full'
    win._persist_sidebar_mode(False)
    assert win.config.settings['ui.sidebar_mode'] == 'full'


def test_set_sidebar_minimal_true_is_noop():
    win = SimpleNamespace(_sidebar_minimal=False)
    MainWindow.set_sidebar_minimal(win, True)
    assert win._sidebar_minimal is False
