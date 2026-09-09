"""Hover-action slots against real GTK: sizing, and the hover query that
decides when the button is up.

Both ``GroupRow`` (split-view) and ``ConnectionRow`` (Manage Files) park the
button in a stack whose other page is empty, so the row hands the button its
*width* only while it is up but keeps its *height* either way — GTK sizing
behaviour, measured here rather than asserted against mocks. The row also asks
a real ``GtkEventControllerMotion`` where the pointer is, and mocks answer any
method name you invent, so that call is made against a real controller here
too.

Runs only under the on-demand GUI harness (skipped headless / in CI).
"""
import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, _GLib = requires_gui()

pytestmark = pytest.mark.gui


def _slot():
    from sshpilot import sidebar

    button = Gtk.Button(icon_name='folder-symbolic')
    button.add_css_class('flat')
    return sidebar._make_row_action_slot(button), sidebar


def _measure(stack, page):
    stack.set_visible_child_name(page)
    return (stack.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
            stack.measure(Gtk.Orientation.VERTICAL, -1)[1])


def test_the_empty_page_keeps_the_height_and_gives_up_the_width():
    stack, sidebar = _slot()

    button_w, button_h = _measure(stack, sidebar.ROW_ACTION_SLOT_BUTTON)
    empty_w, empty_h = _measure(stack, sidebar.ROW_ACTION_SLOT_EMPTY)

    # Height is what keeps the row from resizing under the pointer.
    assert empty_h == button_h > 0
    # Width is what the name / group title gets back at rest.
    assert empty_w == 0
    assert button_w > 0


def test_the_button_is_what_the_name_was_paying_for():
    """The reclaimed width is worth reclaiming: a whole icon button of it."""
    stack, sidebar = _slot()

    button_w, _ = _measure(stack, sidebar.ROW_ACTION_SLOT_BUTTON)

    assert button_w >= 24


def test_the_row_asks_a_real_controller_where_the_pointer_is():
    """Against a mock any spelling "works"; against GTK only the real one does.

    ``contains-pointer`` is read with ``contains_pointer()`` — the predicate,
    not a ``get_`` property getter. Getting that wrong silently pins the row to
    "pointer is elsewhere", which is how the reveal dies.
    """
    from sshpilot import sidebar

    row = sidebar.ConnectionRow.__new__(sidebar.ConnectionRow)
    row._hover_controller = Gtk.EventControllerMotion()

    # No pointer has ever been near this controller, so the answer is False --
    # what matters is that asking works at all.
    assert sidebar.ConnectionRow._pointer_is_on_row(row) is False


def test_a_row_with_no_controller_yet_reports_no_pointer():
    from sshpilot import sidebar

    row = sidebar.ConnectionRow.__new__(sidebar.ConnectionRow)

    assert sidebar.ConnectionRow._pointer_is_on_row(row) is False


def test_the_group_row_asks_the_same_real_controller_question():
    """The split-view action shares the query, so it shares the guard."""
    from sshpilot import sidebar

    row = sidebar.GroupRow.__new__(sidebar.GroupRow)
    row._hover_controller = Gtk.EventControllerMotion()

    assert sidebar.GroupRow._pointer_is_on_row(row) is False
