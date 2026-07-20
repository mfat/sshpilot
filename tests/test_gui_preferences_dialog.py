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
    from sshpilot import bitwarden_setup, rbw_setup, secret_unlock_dialog

    prefs = _open_preferences(gui)
    assert prefs is not None

    for mod in (bitwarden_setup, rbw_setup, secret_unlock_dialog):
        resolved = mod._parent_window(prefs)
        assert isinstance(resolved, Gtk.Window), f'{mod.__name__} resolved {type(resolved)}'
        assert resolved is gui.window
        # A real window passes through untouched, None stays None.
        assert mod._parent_window(gui.window) is gui.window
        assert mod._parent_window(None) is None

    # Gtk.FileDialog rejects a non-window parent outright.
    assert isinstance(prefs.get_root(), Gtk.Window)
    Gtk.FileDialog().open(prefs.get_root(), None, lambda *a: None)
