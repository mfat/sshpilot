"""GUI regression tests for issue #1170.

Adw.TabView has its own built-in keyboard accelerators (Ctrl+PageUp/Down,
Ctrl+Shift+PageUp/Down, Ctrl+Tab, ...) that are completely independent of SSH
Pilot's configurable ``tab-next`` / ``tab-prev`` / ``tab-move-left`` /
``tab-move-right`` actions. Before this fix, disabling one of those actions in
preferences only cleared SSH Pilot's own accelerator — Adw.TabView kept
handling the key itself, so it never reached the focused VTE terminal (e.g.
Ctrl+PageUp/Down in vim/nano).

The fix makes SSH Pilot the sole owner of tab-navigation keys by disabling all
Adw.TabView built-in shortcuts (``Adw.TabViewShortcuts.NONE``) on both the
outer window tab view and the inner split-pane tab view, so an explicitly
disabled action leaves the key completely unclaimed.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from gi.repository import Adw

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _activate_app_action(gui, name):
    action = gui.app.lookup_action(name)
    assert action is not None, f'app action {name!r} not registered'
    action.activate(None)
    gui.pump(200)


def _set_override(gui, name, accels):
    gui.app.config.set_shortcut_override(name, accels)
    gui.app.apply_shortcut_overrides()


def _clear_override(gui, name):
    gui.app.config.set_shortcut_override(name, None)
    gui.app.apply_shortcut_overrides()


def test_outer_tab_view_has_no_builtin_shortcuts(gui):
    """SSH Pilot must be the sole owner of tab-navigation keys: the outer
    Adw.TabView should not have any of its own built-in accelerators, or they
    would shadow the configurable actions (and the shortcut editor) below."""
    assert gui.window.tab_view.get_shortcuts() == Adw.TabViewShortcuts.NONE


def test_configured_tab_next_and_prev_switch_tabs(gui):
    gui.open_local_tabs(2)
    pages = gui.user_pages()
    assert len(pages) == 2
    gui.window.tab_view.set_selected_page(pages[0])
    gui.pump(100)

    _activate_app_action(gui, 'tab-next')
    assert gui.window.tab_view.get_selected_page() == pages[1]

    _activate_app_action(gui, 'tab-prev')
    assert gui.window.tab_view.get_selected_page() == pages[0]


def test_configured_move_left_and_right_reorder_tabs(gui):
    gui.open_local_tabs(2)
    pages = gui.user_pages()
    assert len(pages) == 2
    gui.window.tab_view.set_selected_page(pages[1])
    gui.pump(100)

    _activate_app_action(gui, 'tab-move-left')
    assert gui.window.tab_view.get_page_position(pages[1]) == 0

    _activate_app_action(gui, 'tab-move-right')
    assert gui.window.tab_view.get_page_position(pages[1]) == 1


def test_disabled_tab_next_is_not_consumed_by_app_or_tab_view(gui):
    """Disabling tab-next must remove SSH Pilot's own accelerator, and there
    must be no other handler (Adw.TabView built-in) left to consume the key
    on its behalf — the event is then free to reach VTE untouched."""
    try:
        _set_override(gui, 'tab-next', [])
        assert gui.app.get_accels_for_action('app.tab-next') == []
        # Nothing else claims the combo: the outer tab view has zero builtin
        # shortcuts regardless of the action's enabled state.
        assert gui.window.tab_view.get_shortcuts() == Adw.TabViewShortcuts.NONE
    finally:
        _clear_override(gui, 'tab-next')


def test_disabled_tab_prev_is_not_consumed_by_app_or_tab_view(gui):
    try:
        _set_override(gui, 'tab-prev', [])
        assert gui.app.get_accels_for_action('app.tab-prev') == []
        assert gui.window.tab_view.get_shortcuts() == Adw.TabViewShortcuts.NONE
    finally:
        _clear_override(gui, 'tab-prev')


def test_disabled_move_shortcuts_are_not_consumed(gui):
    try:
        _set_override(gui, 'tab-move-left', [])
        _set_override(gui, 'tab-move-right', [])
        assert gui.app.get_accels_for_action('app.tab-move-left') == []
        assert gui.app.get_accels_for_action('app.tab-move-right') == []
        assert gui.window.tab_view.get_shortcuts() == Adw.TabViewShortcuts.NONE
    finally:
        _clear_override(gui, 'tab-move-left')
        _clear_override(gui, 'tab-move-right')


def test_alt_arrow_tab_nav_respects_terminal_pass_through(gui):
    """Issue found while reviewing #1170: the hardcoded Alt+Left/Right
    tab-navigation helper isn't part of the shortcut registry, so it kept
    consuming the key (and switching tabs) even with Terminal Shortcut
    Pass-through active — defeating pass-through for a common readline/vim
    word-navigation combo. It must now check accelerators_enabled itself."""
    gui.open_local_tabs(2)
    pages = gui.user_pages()
    gui.window.tab_view.set_selected_page(pages[0])
    gui.pump(100)

    app = gui.app
    app._accelerators_enabled = False
    app._update_accelerators_enabled_flag()
    try:
        consumed = gui.window._on_tab_nav_shortcut(1)
        assert consumed is False
        assert gui.window.tab_view.get_selected_page() == pages[0]
    finally:
        app._accelerators_enabled = True
        app._update_accelerators_enabled_flag()

    consumed = gui.window._on_tab_nav_shortcut(1)
    assert consumed is True
    assert gui.window.tab_view.get_selected_page() == pages[1]


def test_single_tab_next_is_still_owned_by_the_action_when_enabled(gui):
    """GNOME Terminal semantics: an *enabled* shortcut whose action is
    temporarily unavailable (only one tab open) still belongs to SSH Pilot —
    it must not silently fall through to the terminal. The action fires (and
    is a no-op) rather than leaking the key."""
    gui.open_local_tabs(1)
    pages = gui.user_pages()
    assert len(pages) == 1
    gui.window.tab_view.set_selected_page(pages[0])
    gui.pump(100)

    assert gui.app.get_effective_shortcuts('tab-next') == ['<primary>Page_Down']
    _activate_app_action(gui, 'tab-next')
    assert gui.window.tab_view.get_selected_page() == pages[0]


def test_restoring_default_shortcut_restores_navigation(gui):
    gui.open_local_tabs(2)
    pages = gui.user_pages()
    gui.window.tab_view.set_selected_page(pages[0])
    gui.pump(100)

    try:
        _set_override(gui, 'tab-next', [])
        assert gui.app.get_accels_for_action('app.tab-next') == []

        _clear_override(gui, 'tab-next')
        assert gui.app.get_effective_shortcuts('tab-next') == ['<primary>Page_Down']

        _activate_app_action(gui, 'tab-next')
        assert gui.window.tab_view.get_selected_page() == pages[1]
    finally:
        _clear_override(gui, 'tab-next')


def test_shortcut_override_persists_across_reapply(gui):
    """Simulates reload/restart: overrides survive a fresh
    ``apply_shortcut_overrides()`` pass reading back from config."""
    try:
        _set_override(gui, 'tab-next', [])
        gui.app.apply_shortcut_overrides()
        assert gui.app.get_effective_shortcuts('tab-next') == []
        assert gui.app.get_accels_for_action('app.tab-next') == []
    finally:
        _clear_override(gui, 'tab-next')
    gui.app.apply_shortcut_overrides()
    assert gui.app.get_effective_shortcuts('tab-next') == ['<primary>Page_Down']


def test_split_pane_inner_tab_view_has_no_builtin_shortcuts(gui):
    """The inner Adw.TabView used to stack sub-tabs within a split pane has
    the same duplicate-handler hazard and must also own none of its own
    keyboard shortcuts."""
    _activate_app_action(gui, 'new-split-view-tab')
    try:
        svt_page = gui.window.tab_view.get_selected_page()
        svt = svt_page.get_child()
        assert svt._panes, 'new split-view tab has no panes'
        for pane in svt._panes:
            assert pane._inner_tab_view.get_shortcuts() == Adw.TabViewShortcuts.NONE
    finally:
        gui.window.tab_view.close_page(gui.window.tab_view.get_selected_page())
        gui.pump(200)
