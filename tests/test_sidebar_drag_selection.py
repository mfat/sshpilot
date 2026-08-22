"""A sidebar drag must not cost the user their selection or their focus.

``_on_connection_list_motion`` switches the ListBox to ``SelectionMode.NONE`` so
a drag cannot change the selection. That clears the selection outright, and
restoring ``MULTIPLE`` does not bring it back, so before this pairing existed
every drag ended with nothing selected — and because ``rebuild_connection_list``
samples the selection *after* the drag already wiped it, the rebuild had nothing
to restore and GTK parked the cursor on row 0.
"""

from types import SimpleNamespace

import pytest

from gi.repository import Gtk

import sshpilot.sidebar as sidebar_module
from sshpilot.sidebar import (
    _restore_sidebar_selection,
    _suspend_sidebar_selection,
)

try:
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    MainWindow = None


class _Row:
    """ListBoxRow double that tracks selection and parenting."""

    def __init__(self, name, parented=True):
        self.name = name
        self._parent = object() if parented else None

    def get_parent(self):
        return self._parent

    def retire(self):
        self._parent = None


class _ListBox:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.selected = list(rows)
        self.mode = "MULTIPLE"

    def get_selected_rows(self):
        return list(self.selected)

    def select_row(self, row):
        if row not in self.selected:
            self.selected.append(row)

    def set_selection_mode(self, mode):
        self.mode = mode
        # The real GTK behaviour this whole pairing exists to compensate for.
        if mode is Gtk.SelectionMode.NONE:
            self.selected = []


def _window(rows):
    return SimpleNamespace(connection_list=_ListBox(rows))


def test_suspending_selection_stashes_what_gtk_is_about_to_clear():
    alpha, beta = _Row("alpha"), _Row("beta")
    window = _window([alpha, beta])

    _suspend_sidebar_selection(window)

    # GTK really did drop it...
    assert window.connection_list.get_selected_rows() == []
    # ...but it is not lost.
    assert window._selection_before_drag == [alpha, beta]


def test_restoring_puts_the_selection_back():
    alpha, beta = _Row("alpha"), _Row("beta")
    window = _window([alpha, beta])

    _suspend_sidebar_selection(window)
    _restore_sidebar_selection(window)

    assert window.connection_list.get_selected_rows() == [alpha, beta]
    assert window._selection_before_drag is None


def test_rows_retired_during_the_drag_are_not_re_selected():
    alpha, beta = _Row("alpha"), _Row("beta")
    window = _window([alpha, beta])

    _suspend_sidebar_selection(window)
    beta.retire()  # e.g. the drop moved it and its row was replaced
    _restore_sidebar_selection(window)

    assert window.connection_list.get_selected_rows() == [alpha]


def test_restore_without_a_suspend_is_a_no_op_beyond_the_mode():
    window = _window([])
    window.connection_list.selected = []

    _restore_sidebar_selection(window)

    assert window.connection_list.get_selected_rows() == []
    assert window._selection_before_drag is None


def test_restore_is_idempotent():
    alpha = _Row("alpha")
    window = _window([alpha])

    _suspend_sidebar_selection(window)
    _restore_sidebar_selection(window)
    _restore_sidebar_selection(window)

    assert window.connection_list.get_selected_rows() == [alpha]


def test_every_drag_teardown_path_restores_the_selection():
    """Leave, drag-end and the drag-session reset must all pair with suspend."""
    source = sidebar_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    def _calls(name):
        return text.count(f"{name}(window)") - text.count(f"def {name}(window)")

    # Exactly one place blacks the selection out, and every teardown path goes
    # back through the paired restore rather than hand-rolling the mode.
    assert _calls("_suspend_sidebar_selection") == 1
    assert text.count("Gtk.SelectionMode.NONE") == 1
    assert _calls("_restore_sidebar_selection") == 4
    # The only bare MULTIPLE left is inside the restore helper, plus the
    # sidebar's own initial construction.
    assert text.count("set_selection_mode(Gtk.SelectionMode.MULTIPLE)") == 2


@pytest.mark.skipif(MainWindow is None, reason="GTK stubs unavailable")
def test_focus_is_only_restored_when_a_row_had_it():
    """_finish_rebuild must not steal focus from the search entry."""
    focused = []

    class _FocusRow:
        _group_id = None

        def grab_focus(self):
            focused.append(self)

    row = _FocusRow()
    connection = object()

    def _make_window(had_focus):
        return SimpleNamespace(
            _ungrouped_area_row=None,
            _sidebar_minimal=False,
            _search_popup=None,
            connection_rows={connection: [row]},
            connection_list=SimpleNamespace(select_row=lambda _r: None),
            connection_manager=SimpleNamespace(
                get_connection_by_uuid=lambda _u: connection
            ),
            connection_scrolled=None,
            _rebuild_had_row_focus=had_focus,
        )

    MainWindow._finish_rebuild(_make_window(False), None, [("c1", None)])
    assert focused == [], "focus must not move when no row held it"

    MainWindow._finish_rebuild(_make_window(True), None, [("c1", None)])
    assert focused == [row], "the re-selected row should get focus back"
