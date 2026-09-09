"""Group-row split-view and connection-row Manage Files: reserved by default,
shed when the sidebar is too narrow to pay for them.

The group row is what sets the sidebar's measured minimum width, and a reserved
34px button is most of it — so the width at which the button goes is also the
width at which the divider can keep going. Connection Manage Files uses the
same threshold; the strip still keeps Manage Files reserved.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _group_row():
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.GroupRow.__new__(mod.GroupRow)
    row._compact = False
    row._is_hovering_row = False
    row._actions_reserved = True
    row.split_view_button = MagicMock(name='split_view_button')
    return row, mod


def _connection_row(*, callback=True):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row._compact = False
    row._is_hovering = False
    row._actions_reserved = True
    row._action_height_floor_on = False
    row._action_button_height_px = 0
    row._file_manager_callback = MagicMock() if callback else None
    row.file_manager_button = MagicMock(name='file_manager_button')
    row.file_manager_button.measure.return_value = (34, 34, -1, -1)
    row._content_box = MagicMock(name='_content_box')
    return row, mod


def test_hover_only_changes_opacity_so_the_row_never_reflows():
    row, mod = _group_row()

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    row.split_view_button.set_visible.assert_called_with(True)
    row.split_view_button.set_opacity.assert_called_with(1.0)

    row._is_hovering_row = False
    mod.GroupRow._maybe_hide_row_actions(row)
    row.split_view_button.set_visible.assert_called_with(True)
    row.split_view_button.set_opacity.assert_called_with(0.0)


def test_a_narrow_sidebar_sheds_the_button_entirely():
    row, mod = _group_row()

    mod.GroupRow.set_actions_reserved(row, False)

    row.split_view_button.set_visible.assert_called_with(False)
    # And hovering must not bring it back — that would reflow the row.
    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)
    row.split_view_button.set_visible.assert_called_with(False)
    row.split_view_button.set_opacity.assert_called_with(0.0)


def test_widening_restores_the_reservation():
    row, mod = _group_row()
    mod.GroupRow.set_actions_reserved(row, False)

    mod.GroupRow.set_actions_reserved(row, True)

    row.split_view_button.set_visible.assert_called_with(True)


def test_the_strip_keeps_no_row_action():
    row, mod = _group_row()
    row._compact = True

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    row.split_view_button.set_visible.assert_called_with(False)


def test_connection_hover_only_changes_opacity_when_reserved():
    row, mod = _connection_row()

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    row.file_manager_button.set_visible.assert_called_with(True)
    row.file_manager_button.set_opacity.assert_called_with(1.0)
    assert row._action_height_floor_on is False

    row._is_hovering = False
    mod.ConnectionRow._maybe_hide_button(row)
    row.file_manager_button.set_visible.assert_called_with(True)
    row.file_manager_button.set_opacity.assert_called_with(0.0)


def test_connection_narrow_sheds_entirely_like_group_split_view():
    row, mod = _connection_row()

    mod.ConnectionRow.set_actions_reserved(row, False)

    row.file_manager_button.set_visible.assert_called_with(False)
    row.file_manager_button.set_opacity.assert_called_with(0.0)
    row._content_box.set_size_request.assert_called_with(-1, 34)
    assert row._action_height_floor_on is True

    # Hover must not bring it back — same as group split-view.
    mod.ConnectionRow._on_row_enter(row, None, 0, 0)
    row.file_manager_button.set_visible.assert_called_with(False)
    row.file_manager_button.set_opacity.assert_called_with(0.0)


def test_connection_widening_restores_the_reservation():
    row, mod = _connection_row()
    mod.ConnectionRow.set_actions_reserved(row, False)
    row._content_box.reset_mock()

    mod.ConnectionRow.set_actions_reserved(row, True)

    row.file_manager_button.set_visible.assert_called_with(True)
    row._content_box.set_size_request.assert_called_with(-1, -1)
    assert row._action_height_floor_on is False


def test_connection_strip_keeps_manage_files_even_when_shed():
    """Minimal mode always reserves Manage Files; width shedding is full-only."""
    row, mod = _connection_row()
    row._actions_reserved = False
    row._compact = True

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    row.file_manager_button.set_visible.assert_called_with(True)
    row.file_manager_button.set_opacity.assert_called_with(1.0)
    assert row._action_height_floor_on is False


def test_connection_without_callback_never_reserves():
    row, mod = _connection_row(callback=False)

    mod.ConnectionRow._reveal_row_actions(row, True)

    row.file_manager_button.set_visible.assert_called_with(False)
    row.file_manager_button.set_opacity.assert_called_with(0.0)


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
