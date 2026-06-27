"""GUI tests for editable split-view shortcuts (issue #1011).

The split-view shortcuts are now registered in the shortcut registry (so the
editor lists them and they respect config overrides) but are NOT applied as
global accelerators — the SplitViewTab CAPTURE handler triggers them. These
tests assert that registration/override wiring and the editor's editable rows.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


SPLIT_DEFAULTS = {
    'split-focus-left': ['<Control><Alt>h'],
    'split-focus-down': ['<Control><Alt>j'],
    'split-focus-up': ['<Control><Alt>k'],
    'split-focus-right': ['<Control><Alt>l'],
    'split-resize-left': ['<Control><Alt><Shift>h'],
    'split-resize-down': ['<Control><Alt><Shift>j'],
    'split-resize-up': ['<Control><Alt><Shift>k'],
    'split-resize-right': ['<Control><Alt><Shift>l'],
    'split-layout-horizontal': ['<Control><Shift>backslash'],
    'split-layout-vertical': ['<Control><Shift>minus'],
    'split-add-pane': ['<Control><Shift>n'],
}


def test_split_actions_registered_with_defaults(gui):
    app = gui.app
    defaults = app.get_registered_shortcut_defaults()
    order = app.get_registered_action_order()
    for name, accel in SPLIT_DEFAULTS.items():
        assert name in order, f'{name} missing from action order'
        assert defaults.get(name) == accel
        assert app.get_effective_shortcuts(name) == accel


def test_split_actions_not_claimed_globally(gui):
    """Custom shortcuts must never become global accelerators (would block the
    same keys from reaching a normal terminal outside a split view)."""
    app = gui.app
    for name in SPLIT_DEFAULTS:
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
    assert app.get_effective_shortcuts(name) == ['<Control><Alt>h']


def test_split_default_triggers_match_original_keys(gui):
    """The accelerators the CAPTURE handler builds (via Gtk.ShortcutTrigger)
    must resolve to the same keyval+modifiers the old hardcoded handler used."""
    from gi.repository import Gdk, Gtk

    CTRL = Gdk.ModifierType.CONTROL_MASK
    ALT = Gdk.ModifierType.ALT_MASK
    SHIFT = Gdk.ModifierType.SHIFT_MASK
    expected = {
        'split-focus-left': (Gdk.KEY_h, CTRL | ALT),
        'split-focus-down': (Gdk.KEY_j, CTRL | ALT),
        'split-focus-up': (Gdk.KEY_k, CTRL | ALT),
        'split-focus-right': (Gdk.KEY_l, CTRL | ALT),
        'split-resize-left': (Gdk.KEY_h, CTRL | ALT | SHIFT),
        'split-resize-right': (Gdk.KEY_l, CTRL | ALT | SHIFT),
        'split-layout-horizontal': (Gdk.KEY_backslash, CTRL | SHIFT),
        'split-layout-vertical': (Gdk.KEY_minus, CTRL | SHIFT),
        'split-add-pane': (Gdk.KEY_n, CTRL | SHIFT),
    }
    for name, (keyval, mods) in expected.items():
        accel = gui.app.get_effective_shortcuts(name)[0]
        trigger = Gtk.ShortcutTrigger.parse_string(accel)
        assert trigger is not None, f'{name}: {accel!r} did not parse'
        assert trigger.get_keyval() == keyval, name
        assert trigger.get_modifiers() == mods, name


def test_editor_lists_split_actions_as_editable(gui):
    from sshpilot.shortcut_editor import ShortcutsPreferencesPage

    page = ShortcutsPreferencesPage(
        parent_widget=gui.window, app=gui.app, config=gui.app.config
    )
    for name in SPLIT_DEFAULTS:
        assert name in page._action_names, f'{name} not collected by editor'
        row_data = page._rows.get(name)
        assert row_data is not None, f'no editable row for {name}'
        # Editable rows carry an assign button and an enable switch.
        assert row_data.get('assign_button') is not None
        assert row_data.get('switch') is not None
