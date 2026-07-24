"""GUI tests for the safe-by-default shortcut set (Linux/Windows).

Conflict-prone defaults are disabled ([]) or rebound to safe combos, while the
GNOME Terminal/Ptyxis-style ones stay enabled. Disabled actions remain listed
and assignable in the editor. macOS keeps its Cmd defaults, so this module only
asserts the Linux/Windows (`<primary>`) values and skips on macOS.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from gi.repository import Gdk, Gtk

from sshpilot.platform_utils import is_macos
from sshpilot.shortcut_utils import DOUBLE_SHIFT_SHORTCUT
from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

if is_macos():
    pytest.skip('Linux/Windows default-shortcut policy only', allow_module_level=True)

pytestmark = pytest.mark.gui


# Safe shortcuts kept enabled (unchanged).
KEEP = {
    'quit': ['<primary><shift>q'],
    'tab-close': ['<primary><shift>w'],
    'local-terminal': ['<primary><shift>t'],
    'terminal-search': ['<primary><shift>f'],
    'tab-next': ['<primary>Page_Down'],
    'tab-prev': ['<primary>Page_Up'],
    'tab-move-left': ['<primary><shift>Page_Up'],
    'tab-move-right': ['<primary><shift>Page_Down'],
    'tab-overview': ['<primary><shift>Tab'],
    'preferences': ['<primary>comma'],
    'shortcuts': ['<primary>question'],
    'help': ['F1'],
    'toggle_sidebar': ['F9'],
    'new-key': ['<primary><shift>k'],
    'manage-files': ['<primary><shift>o'],
    'broadcast-command': ['<primary><shift>b'],
    'search': ['<primary>f'],  # kept on Ctrl+F by request
    'omnisearch': [DOUBLE_SHIFT_SHORTCUT],
}

# Conflict-prone defaults rebound to safe combos.
REBIND = {
    'new-connection': ['<primary><shift>n'],
}

# Disabled by default (still listed/assignable).
DISABLED = [
    'open-new-connection-tab',
    'toggle-list',
    'edit-ssh-config',
    'new-split-view-tab',
    'toggle-command-blocks',
]


def test_kept_shortcuts_unchanged(gui):
    defaults = gui.app.get_registered_shortcut_defaults()
    for name, accel in KEEP.items():
        if name not in defaults:
            continue  # e.g. manage-files hidden when file-manager options off
        assert gui.app.get_effective_shortcuts(name) == accel, name


def test_rebound_shortcuts(gui):
    for name, accel in REBIND.items():
        assert gui.app.get_effective_shortcuts(name) == accel, name


def test_disabled_shortcuts_are_empty_but_listed(gui):
    from sshpilot.shortcut_editor import ShortcutsPreferencesPage

    app = gui.app
    for name in DISABLED:
        assert app.get_effective_shortcuts(name) == [], name
        assert app.get_accels_for_action(f'win.{name}') == []
        assert app.get_accels_for_action(f'app.{name}') == []

    page = ShortcutsPreferencesPage(parent_widget=gui.window, app=app, config=app.config)
    for name in DISABLED:
        assert name in page._action_names, f'{name} not listed in editor'


def test_disabled_action_can_be_assigned(gui):
    app = gui.app
    name = 'edit-ssh-config'
    try:
        app.config.set_shortcut_override(name, ['<primary><shift>e'])
        app.apply_shortcut_overrides()
        assert app.get_effective_shortcuts(name) == ['<primary><shift>e']
        # edit-ssh-config is an app action (create_action), so the override
        # becomes a real accelerator on app.<name> (GTK normalizes the string,
        # e.g. '<primary><shift>e' -> '<Shift><Control>e', so just assert it was
        # applied — non-empty where it was [] before).
        assert app.get_accels_for_action(f'app.{name}'), 'override not applied'
    finally:
        app.config.set_shortcut_override(name, None)
        app.apply_shortcut_overrides()
    assert app.get_effective_shortcuts(name) == []


def _walk_widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk_widgets(child)
        child = child.get_next_sibling()


def test_search_and_omnisearch_have_separate_editor_and_viewer_entries(gui):
    from sshpilot.shortcut_editor import ShortcutsPreferencesPage

    page = ShortcutsPreferencesPage(
        parent_widget=gui.window, app=gui.app, config=gui.app.config
    )
    assert page._rows['search']['row'].get_title() == 'Search'
    assert page._rows['omnisearch']['row'].get_title() == 'Omnisearch'
    assert page._rows['omnisearch']['row'].get_subtitle() == 'Double Shift'

    viewer = gui.window._build_shortcuts_window()
    titles = {
        widget.get_property('title')
        for widget in _walk_widgets(viewer)
        if isinstance(widget, Gtk.ShortcutsShortcut)
    }
    assert 'Search' in titles
    assert 'Omnisearch' in titles


def test_double_shift_opens_omnisearch(gui, monkeypatch):
    win = gui.window
    times = iter((1.0, 1.05, 1.2, 1.25))
    monkeypatch.setattr(
        win, '_shortcut_event_time', lambda: next(times)
    )
    win._omni_search.dismiss(clear=True)

    win._on_omnisearch_key_pressed(None, Gdk.KEY_Shift_L, 0, 0)
    win._on_omnisearch_key_released(None, Gdk.KEY_Shift_L, 0, 0)
    win._on_omnisearch_key_pressed(None, Gdk.KEY_Shift_R, 0, 0)
    win._on_omnisearch_key_released(None, Gdk.KEY_Shift_R, 0, 0)
    gui.pump(100)

    assert win._omni_search.popup.visible
