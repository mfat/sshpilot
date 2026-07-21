"""GUI regression tests for Preferences as a main-window Settings mode.

Settings is an ``Adw.NavigationPage`` pushed onto the main ``Adw.NavigationView``,
not an ``Adw.Dialog`` and not a tab. Easy silent regressions: failing to push,
not flushing on pop, or parenting dialogs to a non-window.

These boot the real app and open the real Settings page. Opt-in only:
SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _open_preferences(gui, page_id=None):
    gui.window.show_preferences(page_id)
    gui.pump(500)
    return getattr(gui.window, '_preferences_window', None)


def test_preferences_pushes_onto_navigation_view(gui):
    from gi.repository import Adw

    prefs = _open_preferences(gui)
    assert prefs is not None, 'show_preferences() did not store the page'
    assert isinstance(prefs, Adw.NavigationPage), f'expected NavigationPage, got {type(prefs)}'
    assert gui.window.nav_view.get_visible_page() is prefs
    assert prefs.get_root() is gui.window
    assert gui.window.is_preferences_visible()


def test_preferences_is_reused_and_pops_back_to_work(gui):
    first = _open_preferences(gui)
    assert first is not None

    again = _open_preferences(gui)
    assert again is first

    assert gui.window.leave_preferences()
    gui.pump(500)
    assert gui.window.nav_view.get_visible_page() is gui.window._work_page
    assert not gui.window.is_preferences_visible()
    # Instance is kept for reuse on the next open.
    assert getattr(gui.window, '_preferences_window', None) is first


def test_preferences_select_page_deep_link(gui):
    prefs = _open_preferences(gui, page_id='plugins')
    assert prefs is not None
    assert prefs._selected_page_name == 'plugins'
    assert prefs.content_stack.get_visible_child_name() == 'plugins'


def test_dialogs_parented_to_preferences_get_a_window(gui):
    """Anything that takes Preferences as a parent must still resolve a window."""
    from gi.repository import Gtk
    from sshpilot.window_dialogs import parent_window

    prefs = _open_preferences(gui)
    assert prefs is not None

    resolved = parent_window(prefs)
    assert isinstance(resolved, Gtk.Window), f'resolved {type(resolved)}'
    assert resolved is gui.window
    assert parent_window(gui.window) is gui.window
    assert parent_window(None) is None
    assert isinstance(prefs.get_root(), Gtk.Window)


def test_every_page_widget_resolves_a_window_root(gui):
    """Page code parents dialogs with self.get_root(); that must be MainWindow."""
    from gi.repository import Gtk

    prefs = _open_preferences(gui)
    assert prefs is not None

    def walk(widget):
        yield widget
        child = widget.get_first_child()
        while child is not None:
            yield from walk(child)
            child = child.get_next_sibling()

    widgets = list(walk(prefs))
    assert len(widgets) > 50, f'only {len(widgets)} widgets; page tree did not build'

    for w in widgets:
        root = w.get_root()
        assert isinstance(root, Gtk.Window), f'{type(w).__name__} resolved {type(root)}'
        assert root is gui.window

    page = getattr(prefs, 'shortcuts_editor_page', None)
    if page is not None:
        assert page._transient_parent is None or isinstance(page._transient_parent, Gtk.Window)


def test_collapsed_split_shows_content_on_page_change(gui):
    """Narrow layout: picking a category must show the detail page."""
    prefs = _open_preferences(gui)
    split = prefs.split_view

    split.set_collapsed(True)
    split.set_show_content(False)
    gui.pump(200)
    assert not split.get_show_content()

    current = prefs.sidebar.get_selected_row()
    other = next(
        r for i in range(10)
        if (r := prefs.sidebar.get_row_at_index(i)) is not None and r is not current
    )
    prefs.sidebar.select_row(other)
    gui.pump(200)
    assert split.get_show_content(), 'choosing a page did not show content'

    # Re-selecting the current page must not bounce show_content when already on list.
    split.set_show_content(False)
    gui.pump(100)
    prefs.sidebar.select_row(other)
    gui.pump(200)
    assert not split.get_show_content(), 'reselecting the same row forced content'


def test_dialog_presented_on_the_window_while_in_settings(gui):
    """A dialog parented to MainWindow still works while Settings mode is open."""
    from gi.repository import Adw

    prefs = _open_preferences(gui)
    assert gui.window.is_preferences_visible()

    alert = Adw.AlertDialog(heading='Unlock', body='parented to the window')
    alert.add_response('ok', 'OK')
    alert.present(gui.window)
    gui.pump(300)

    assert alert.get_visible()
    assert gui.window.is_preferences_visible(), 'alert should not leave Settings'
    assert alert.get_root() is gui.window

    def focus_inside_alert():
        widget = gui.window.get_focus()
        while widget is not None:
            if widget is alert:
                return True
            widget = widget.get_parent()
        return False

    for _ in range(20):
        if focus_inside_alert():
            break
        gui.pump(100)
    assert focus_inside_alert(), 'the alert is not the focused (topmost) dialog'

    alert.close()
    gui.pump(200)
