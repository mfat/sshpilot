"""Row hover actions: the group row's split-view button and the connection
row's Manage Files button.

Both park the button in a stack that keeps its height but hands the width to
the name at rest, and both ask GTK where the pointer is rather than keeping a
hover flag — which is what keeps a stray leave from latching the action off.
Below ``_ROW_ACTIONS_MIN_WIDTH`` neither may borrow the width on hover.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _group_row(pointer_on_row=False):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.GroupRow.__new__(mod.GroupRow)
    row._compact = False
    row._actions_reserved = True
    row.split_view_button = MagicMock(name='split_view_button')
    row._split_view_slot = MagicMock(name='split_view_slot')
    row._hover_controller = MagicMock(name='hover_controller')
    row._hover_controller.contains_pointer.return_value = pointer_on_row
    return row, mod


def _group_slot_page(row):
    return row._split_view_slot.set_visible_child_name.call_args.args[0]


def test_the_split_view_action_takes_no_space_until_hovered():
    """The group title is laid out across the whole row at rest; it only pays
    for the button's width while the pointer is on the row."""
    row, mod = _group_row()

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)
    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON

    mod.GroupRow._maybe_hide_row_actions(row)
    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY


def test_a_stray_leave_does_not_end_the_group_row_hover():
    """Same latch the connection rows had: a leave that fires while the pointer
    is still on the row — crossing onto the button, a grab, a popup — must not
    put the action down until the pointer has really gone."""
    row, mod = _group_row(pointer_on_row=True)
    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    mod.GroupRow._on_row_leave_actions(row, None)
    mod.GroupRow._maybe_hide_row_actions(row)

    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_motion_re_arms_a_group_row_that_lost_the_reveal():
    """A row rebuilt under a pointer that never moved gets no crossing event."""
    row, mod = _group_row(pointer_on_row=True)
    mod.GroupRow._reveal_row_actions(row, False)

    mod.GroupRow._on_row_motion_actions(row, None, 4, 4)

    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_a_rebuilt_group_row_under_the_pointer_keeps_the_action():
    row, mod = _group_row(pointer_on_row=True)
    row._actions_reserved = False

    mod.GroupRow.set_actions_reserved(row, True)

    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_a_narrow_sidebar_sheds_the_button_entirely():
    row, mod = _group_row()

    mod.GroupRow.set_actions_reserved(row, False)

    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY
    # And hovering must not bring it back — that would ellipsise the title.
    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)
    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY


def test_widening_restores_the_hover_action():
    row, mod = _group_row(pointer_on_row=True)
    mod.GroupRow.set_actions_reserved(row, False)

    mod.GroupRow.set_actions_reserved(row, True)

    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_the_strip_keeps_no_row_action():
    row, mod = _group_row()
    row._compact = True

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    assert _group_slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY


def _connection_row(callback=True, pointer_on_row=False):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row._compact = False
    row._actions_affordable = True
    row._file_manager_callback = MagicMock() if callback else None
    row.file_manager_button = MagicMock(name='file_manager_button')
    row._file_manager_slot = MagicMock(name='file_manager_slot')
    row._hover_controller = MagicMock(name='hover_controller')
    row._hover_controller.contains_pointer.return_value = pointer_on_row
    return row, mod


def _slot_page(row):
    return row._file_manager_slot.set_visible_child_name.call_args.args[0]


def test_the_manage_files_action_takes_no_space_until_hovered():
    """The connection name is laid out across the whole row at rest; it only
    pays for the button's width while the pointer is on the row."""
    row, mod = _connection_row()

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)
    assert _slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON

    mod.ConnectionRow._maybe_hide_button(row)
    assert _slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY


def test_a_stray_leave_does_not_end_the_hover():
    """GTK is asked where the pointer is before the button goes down. A leave
    that fires while the pointer is still on the row — crossing onto the button
    it just revealed, a grab, a popup — used to latch the reveal off until the
    pointer left the row and came back."""
    row, mod = _connection_row(pointer_on_row=True)
    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    mod.ConnectionRow._on_row_leave(row, None)
    mod.ConnectionRow._maybe_hide_button(row)

    assert _slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_motion_re_arms_a_row_that_lost_the_reveal():
    """A row rebuilt under a pointer that never moved gets no crossing event,
    so plain motion has to bring the action back."""
    row, mod = _connection_row(pointer_on_row=True)
    row._file_manager_slot.set_visible_child_name(mod.ROW_ACTION_SLOT_EMPTY)

    mod.ConnectionRow._on_row_motion(row, None, 4, 4)

    assert _slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_a_rebuilt_row_under_the_pointer_keeps_the_action():
    """Re-applying the width policy (every rebuild does) asks GTK where the
    pointer is, so a fresh row built under it shows the action straight away."""
    row, mod = _connection_row(pointer_on_row=True)

    mod.ConnectionRow.set_actions_reserved(row, True)

    assert _slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_a_row_without_a_file_manager_never_shows_the_action():
    row, mod = _connection_row(callback=False)

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    assert _slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY


def test_a_narrow_sidebar_keeps_the_manage_files_action_down():
    """Below the threshold the name has no width to lend on hover, so the row
    drops the action and the file manager is reached from the context menu."""
    row, mod = _connection_row(pointer_on_row=True)

    mod.ConnectionRow.set_actions_reserved(row, False)
    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    assert _slot_page(row) == mod.ROW_ACTION_SLOT_EMPTY

    # Widening while the pointer is still on the row brings it straight back.
    mod.ConnectionRow.set_actions_reserved(row, True)
    assert _slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def test_the_strip_keeps_its_trimmed_manage_files_action():
    """The compact strip is narrower than the threshold but keeps the action —
    it is the only way to the file manager without leaving minimal mode."""
    row, mod = _connection_row()
    row._compact = True

    mod.ConnectionRow.set_actions_reserved(row, False)
    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    assert _slot_page(row) == mod.ROW_ACTION_SLOT_BUTTON


def _window(width):
    win_mod = importlib.import_module('sshpilot.window')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)
    win._get_sidebar_width = lambda: width
    rows = [SimpleNamespace(set_actions_reserved=MagicMock()) for _ in range(2)]
    rows[0].get_next_sibling = lambda: rows[1]
    rows[1].get_next_sibling = lambda: None
    lb = MagicMock(name='connection_list')
    lb.get_first_child.return_value = rows[0]
    win.connection_list = lb
    return win, win_mod, rows


def test_rows_reserve_above_the_threshold():
    win, mod, rows = _window(200)
    mod.MainWindow._apply_sidebar_row_actions(win)
    for r in rows:
        r.set_actions_reserved.assert_called_once_with(True)


def test_rows_shed_below_the_threshold():
    win, mod, rows = _window(170)
    mod.MainWindow._apply_sidebar_row_actions(win)
    for r in rows:
        r.set_actions_reserved.assert_called_once_with(False)


def test_the_threshold_clears_the_floor_the_reservation_produces():
    """Shedding must not fight the width it enables: the threshold has to sit
    above the sidebar minimum a reserved button produces (~150px)."""
    mod = importlib.import_module('sshpilot.window')
    assert mod._ROW_ACTIONS_MIN_WIDTH > 150


def test_repeat_calls_do_not_walk_the_list_again():
    win, mod, rows = _window(200)
    mod.MainWindow._apply_sidebar_row_actions(win)
    mod.MainWindow._apply_sidebar_row_actions(win)
    for r in rows:
        assert r.set_actions_reserved.call_count == 1
    mod.MainWindow._apply_sidebar_row_actions(win, force=True)
    for r in rows:
        assert r.set_actions_reserved.call_count == 2
