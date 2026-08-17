"""GUI tests for split-view shortcuts (issue #1011 + safe-defaults change).

The split-view shortcuts are registered in the shortcut registry (so the editor
lists them and they respect config overrides) but are NOT applied as global
accelerators — the SplitViewTab CAPTURE handler triggers them. They are now
**disabled by default** (empty default) because their original Ctrl+Alt/Ctrl+Shift
combos clashed with CLI tools / keyboard layouts; they remain listed and
assignable.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


SPLIT_NAMES = [
    'split-focus-left', 'split-focus-down', 'split-focus-up', 'split-focus-right',
    'split-resize-left', 'split-resize-down', 'split-resize-up', 'split-resize-right',
    'split-layout-horizontal', 'split-layout-vertical', 'split-add-pane',
]


def test_split_actions_registered_but_disabled(gui):
    app = gui.app
    defaults = app.get_registered_shortcut_defaults()
    order = app.get_registered_action_order()
    for name in SPLIT_NAMES:
        assert name in order, f'{name} missing from action order'
        # Disabled by default == empty list (NOT None — None would hide it).
        assert defaults.get(name) == []
        assert app.get_effective_shortcuts(name) == []


def test_split_actions_not_claimed_globally(gui):
    """Custom shortcuts must never become global accelerators (would block the
    same keys from reaching a normal terminal outside a split view)."""
    app = gui.app
    for name in SPLIT_NAMES:
        assert app.get_accels_for_action(f'win.{name}') == []
        assert app.get_accels_for_action(f'app.{name}') == []


def test_override_applies_and_resets_without_claiming(gui):
    app = gui.app
    name = 'split-focus-left'
    try:
        app.config.set_shortcut_override(name, ['<Control><Alt>Left'])
        app.apply_shortcut_overrides()
        assert app.get_effective_shortcuts(name) == ['<Control><Alt>Left']
        # still not claimed as a global accelerator after re-apply
        assert app.get_accels_for_action(f'win.{name}') == []
    finally:
        app.config.set_shortcut_override(name, None)
        app.apply_shortcut_overrides()
    # Resets to the (disabled) default, not the old hardcoded accelerator.
    assert app.get_effective_shortcuts(name) == []


def test_shortcuts_viewer_omits_disabled_split(gui):
    """The Help → Keyboard Shortcuts viewer builds and omits disabled actions."""
    win = gui.window
    viewer = win._build_shortcuts_window()
    assert viewer is not None
    gui.pump(150)
    current = win._get_safe_current_shortcuts()
    # Disabled split shortcuts resolve to [] and are not shown.
    assert current.get('split-focus-left') == []


def test_editor_lists_disabled_split_as_assignable(gui):
    from sshpilot.shortcut_editor import ShortcutsPreferencesPage

    page = ShortcutsPreferencesPage(
        parent_widget=gui.window, app=gui.app, config=gui.app.config
    )
    for name in SPLIT_NAMES:
        # Disabled ([] default) actions are still listed and assignable.
        assert name in page._action_names, f'{name} not collected by editor'
        row_data = page._rows.get(name)
        assert row_data is not None, f'no editable row for {name}'
        assert row_data.get('assign_button') is not None
        assert row_data.get('switch') is not None


def test_split_action_not_triggered_during_terminal_pass_through(gui):
    """Issue found while reviewing #1170: SplitViewTab._on_key_pressed is a
    CAPTURE-phase handler that matched live key events against the
    user-assigned split shortcut regardless of Terminal Shortcut Pass-through
    ("Disable all keyboard shortcuts, pass all key events directly to
    terminal"). Once a user assigned a combo, pass-through could not free it
    for the terminal. Verify the CAPTURE handler now refuses to consume (or
    fire) the action while accelerators are globally suspended."""
    from gi.repository import Gdk
    from sshpilot.split_view import SplitViewTab

    app = gui.app
    name = 'split-focus-left'
    svt = SplitViewTab(gui.window)
    called = {'n': 0}
    svt._split_actions[name] = lambda: called.__setitem__('n', called['n'] + 1)

    class _FocusStub:
        def get_focus(self_inner):
            return svt

    svt.get_root = lambda: _FocusStub()

    try:
        app.config.set_shortcut_override(name, ['<Control><Alt>Left'])
        app.apply_shortcut_overrides()

        app._accelerators_enabled = False
        app._update_accelerators_enabled_flag()
        try:
            consumed = svt._on_key_pressed(
                None, 0, 0,
                Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK,
            )
        finally:
            app._accelerators_enabled = True
            app._update_accelerators_enabled_flag()

        assert consumed is False, 'pass-through must not be consumed by split-view CAPTURE handler'
        assert called['n'] == 0, 'split action must not fire while pass-through is active'
    finally:
        app.config.set_shortcut_override(name, None)
        app.apply_shortcut_overrides()
