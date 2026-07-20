"""GUI regression tests for the Preferences dialog.

Preferences is an ``Adw.Dialog`` presented over the main window, not a second
top-level ``Adw.Window``. That distinction is easy to regress silently: a
``Gtk.Window``-era call (``present()`` with no parent, ``connect('close-request')``,
``hide()``) raises inside a ``try/except`` and leaves the dialog un-presented or
untracked rather than crashing.

These boot the real app and open the real dialog. Opt-in only:
SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _open_preferences(gui):
    gui.window.show_preferences()
    gui.pump(500)
    return getattr(gui.window, '_preferences_window', None)


def test_preferences_presents_as_a_dialog_over_the_window(gui):
    from gi.repository import Adw

    prefs = _open_preferences(gui)
    assert prefs is not None, 'show_preferences() did not store the dialog'
    assert isinstance(prefs, Adw.Dialog), f'expected an Adw.Dialog, got {type(prefs)}'
    # Presented means realized inside the parent window, so the widget's root is
    # the main window -- this is what `transient_for=self.get_root()` relies on.
    assert prefs.get_root() is gui.window
    assert prefs.get_visible()


def test_preferences_is_reused_and_untracked_when_closed(gui):
    first = _open_preferences(gui)
    assert first is not None

    # Reopening presents the same dialog rather than stacking a second one.
    again = _open_preferences(gui)
    assert again is first

    first.close()
    gui.pump(500)
    # The 'closed' handler must clear the reference; with the old
    # 'close-request' connection this silently never fired.
    assert getattr(gui.window, '_preferences_window', None) is None


def test_preferences_content_size_is_set(gui):
    prefs = _open_preferences(gui)
    # Adw.Dialog sizes its content; default-width/height do not apply.
    assert prefs.get_content_width() == 820
    assert prefs.get_content_height() == 600


def test_dialogs_parented_to_preferences_get_a_window(gui):
    """Anything that takes Preferences as a parent must still resolve a window.

    Preferences is an Adw.Dialog (a widget), so `transient_for=<prefs>` and
    Gtk.FileDialog's parent argument raise TypeError. The helpers on that path
    resolve the widget's root instead — and they must, because most of these
    call sites sit inside try/except and would otherwise fail silently.
    """
    from gi.repository import Gtk
    from sshpilot.window_dialogs import parent_window

    prefs = _open_preferences(gui)
    assert prefs is not None

    resolved = parent_window(prefs)
    assert isinstance(resolved, Gtk.Window), f'resolved {type(resolved)}'
    assert resolved is gui.window
    # A real window passes through untouched, None stays None.
    assert parent_window(gui.window) is gui.window
    assert parent_window(None) is None

    # What the window-only APIs (transient_for, Gtk.FileDialog) will be handed.
    # Deliberately not opening a chooser here: it would leave a native dialog up.
    assert isinstance(prefs.get_root(), Gtk.Window)


def test_every_page_widget_resolves_a_window_root(gui):
    """The general guarantee for dialogs opened from *any* Preferences page.

    Page code parents dialogs with `self.get_root()` (or a helper that ends up
    there). Preferences used to be an Adw.Window, so that root was Preferences
    itself; now it is the MainWindow the dialog is presented in. Both are
    Gtk.Windows, which is why page-level dialog code is unaffected — but only
    if every widget in every page really does resolve to a window.

    Walks the whole realized widget tree of the dialog and asserts it.
    """
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

    # The one page contributed by another module gets an explicit window, not
    # the dialog, so its own transient parent is correct too.
    page = getattr(prefs, 'shortcuts_editor_page', None)
    if page is not None:
        assert page._transient_parent is None or isinstance(page._transient_parent, Gtk.Window)


def test_collapsed_sidebar_can_be_reopened(gui):
    """The narrow layout must not be a dead end.

    Below the breakpoint the sidebar collapses into an overlay, so the header
    toggle is the only way back to the page list. Revealing the sidebar
    re-emits row-selected for the already-selected row, so the dismiss-on-pick
    behaviour must fire on a real page change only — otherwise the sidebar
    slams shut the instant it opens.
    """
    prefs = _open_preferences(gui)
    split = prefs.overlay_split_view

    split.set_collapsed(True)
    split.set_show_sidebar(False)
    gui.pump(200)
    assert prefs.show_sidebar_button.get_visible(), 'no way to reopen the sidebar'

    prefs.show_sidebar_button.set_active(True)
    gui.pump(200)
    assert split.get_show_sidebar(), 'toggle did not reveal the sidebar'

    # Re-selecting the current page keeps it open; a different page dismisses it.
    current = prefs.sidebar.get_selected_row()
    prefs.sidebar.select_row(current)
    gui.pump(200)
    assert split.get_show_sidebar(), 'reselecting the same row closed the sidebar'

    other = next(r for i in range(10)
                 if (r := prefs.sidebar.get_row_at_index(i)) is not None and r is not current)
    prefs.sidebar.select_row(other)
    gui.pump(200)
    assert not split.get_show_sidebar(), 'choosing a page did not dismiss the sidebar'
