"""Drag / expand sidebar mode switches persist ``ui.sidebar_mode``.

A dragged strip used to be transient (Settings owned the resting mode). The
Preferences toggle is gone; the divider is the control, so the mode it sets
must survive restart. Minimize-on-terminal-open stays non-persisted because it
never goes through ``_on_sidebar_drag_mode_switch``.
"""

from types import SimpleNamespace


class _Config:
    def __init__(self, mode='full'):
        self.settings = {'ui.sidebar_mode': mode}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def _window(mode='full', minimal=False):
    """Bare stand-in that only needs the persist + drag-switch methods."""
    from sshpilot.window import MainWindow

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


def test_drag_to_minimal_persists_and_applies():
    win = _window(mode='full', minimal=False)
    win._on_sidebar_drag_mode_switch(True)
    assert win.config.settings['ui.sidebar_mode'] == 'minimal'
    assert win.applied == [(True, False)]


def test_drag_to_full_persists_and_applies():
    win = _window(mode='minimal', minimal=True)
    win._on_sidebar_drag_mode_switch(False)
    assert win.config.settings['ui.sidebar_mode'] == 'full'
    assert win.applied == [(False, False)]


def test_drag_noop_still_persists_when_already_in_mode():
    """Re-dragging into the same mode must still write the resting setting."""
    win = _window(mode='full', minimal=True)
    # Config says full but strip is showing (e.g. minimize-on-connect). A drag
    # that asks for minimal should lock that in even though the UI is already
    # there — otherwise reboot would reopen full.
    win._on_sidebar_drag_mode_switch(True)
    assert win.config.settings['ui.sidebar_mode'] == 'minimal'
    assert win.applied == []


def test_persist_helper_writes_both_modes():
    win = _window()
    win._persist_sidebar_mode(True)
    assert win.config.settings['ui.sidebar_mode'] == 'minimal'
    win._persist_sidebar_mode(False)
    assert win.config.settings['ui.sidebar_mode'] == 'full'
