"""Sidebar row hover actions and narrow-sidebar chrome shedding.

Group rows reserve the split-view button; connection rows reserve Manage Files.
Shedding (preference off, or no callback) must keep the row's height — the
button is taller than the labels — which is why each action lives in a
height-only stack rather than being hidden outright. The group row also sheds
when the sidebar is too narrow to pay for the reserved width.

Below the same narrow threshold, connection rows shed the port-forwarding
indicator so nicknames keep their ``FULL_LABEL_MIN_CHARS`` floor.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _group_row(show_split_view=True):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.GroupRow.__new__(mod.GroupRow)
    row._compact = False
    row._is_hovering_row = False
    row._actions_reserved = True
    row.group_manager = SimpleNamespace(
        config=SimpleNamespace(
            get_setting=lambda key, default=None: (
                show_split_view
                if key == 'ui.sidebar_show_split_view_button'
                else default
            )
        )
    )
    row.split_view_button = MagicMock(name='split_view_button')
    row._split_view_slot = MagicMock(name='split_view_slot')
    return row, mod


def test_hover_only_changes_opacity_so_the_row_never_reflows():
    row, mod = _group_row()

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    row._split_view_slot.set_visible.assert_called_with(True)
    row._split_view_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_BUTTON)
    row.split_view_button.set_opacity.assert_called_with(1.0)

    row._is_hovering_row = False
    mod.GroupRow._maybe_hide_row_actions(row)
    row._split_view_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_BUTTON)
    row.split_view_button.set_opacity.assert_called_with(0.0)


def test_split_view_button_pref_off_keeps_height_without_width():
    """Preferences ▸ Sidebar ▸ Split View Button defaults off: empty slot page
    so the row does not collapse, and no button width is reserved."""
    row, mod = _group_row(show_split_view=False)

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    row._split_view_slot.set_visible.assert_called_with(True)
    row._split_view_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_EMPTY)
    row.split_view_button.set_opacity.assert_called_with(0.0)


def test_a_narrow_sidebar_sheds_the_button_width():
    row, mod = _group_row()

    mod.GroupRow.set_actions_reserved(row, False)

    row._split_view_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_EMPTY)
    # And hovering must not bring the button page back — that would reflow.
    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)
    row._split_view_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_EMPTY)
    row.split_view_button.set_opacity.assert_called_with(0.0)


def test_widening_restores_the_reservation():
    row, mod = _group_row()
    mod.GroupRow.set_actions_reserved(row, False)

    mod.GroupRow.set_actions_reserved(row, True)

    row._split_view_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_BUTTON)


def test_the_strip_keeps_no_row_action():
    row, mod = _group_row()
    row._compact = True

    mod.GroupRow._on_row_enter_actions(row, None, 0, 0)

    row._split_view_slot.set_visible.assert_called_with(False)


def _connection_row(callback=True, show_file_manager=True):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row._compact = False
    row._is_hovering = False
    row._file_manager_callback = MagicMock() if callback else None
    row.config = SimpleNamespace(
        get_setting=lambda key, default=None: (
            show_file_manager
            if key == 'ui.sidebar_show_file_manager_button'
            else default
        )
    )
    row.file_manager_button = MagicMock(name='file_manager_button')
    row._file_manager_slot = MagicMock(name='file_manager_slot')
    return row, mod


def test_manage_files_hover_only_changes_opacity():
    row, mod = _connection_row()

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    row._file_manager_slot.set_visible.assert_called_with(True)
    row._file_manager_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_BUTTON)
    row.file_manager_button.set_opacity.assert_called_with(1.0)

    row._is_hovering = False
    mod.ConnectionRow._maybe_hide_button(row)
    row._file_manager_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_BUTTON)
    row.file_manager_button.set_opacity.assert_called_with(0.0)


def test_file_manager_button_pref_off_keeps_height_without_width():
    """Preferences ▸ Sidebar ▸ File Manager Button off: empty slot page so the
    row does not collapse, and no button width is reserved."""
    row, mod = _connection_row(show_file_manager=False)

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    row._file_manager_slot.set_visible.assert_called_with(True)
    row._file_manager_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_EMPTY)
    row.file_manager_button.set_opacity.assert_called_with(0.0)


def test_a_row_without_a_file_manager_never_shows_the_action():
    row, mod = _connection_row(callback=False)

    mod.ConnectionRow._on_row_enter(row, None, 0, 0)

    row._file_manager_slot.set_visible_child_name.assert_called_with(
        mod.ROW_ACTION_SLOT_EMPTY)
    row.file_manager_button.set_opacity.assert_called_with(0.0)


def _window(width):
    win_mod = importlib.import_module('sshpilot.window')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)
    win._get_sidebar_width = lambda: width
    rows = [
        SimpleNamespace(
            set_actions_reserved=MagicMock(),
            set_indicators_reserved=MagicMock(),
        )
        for _ in range(2)
    ]
    rows[0].get_next_sibling = lambda: rows[1]
    rows[1].get_next_sibling = lambda: None
    lb = MagicMock(name='connection_list')
    lb.get_first_child.return_value = rows[0]
    win.connection_list = lb
    return win, win_mod, rows


def test_rows_reserve_above_the_threshold():
    win, mod, rows = _window(250)
    mod.MainWindow._apply_sidebar_row_actions(win)
    for r in rows:
        r.set_actions_reserved.assert_called_once_with(True)
        r.set_indicators_reserved.assert_called_once_with(True)


def test_rows_shed_below_the_threshold():
    win, mod, rows = _window(220)
    mod.MainWindow._apply_sidebar_row_actions(win)
    for r in rows:
        r.set_actions_reserved.assert_called_once_with(False)
        r.set_indicators_reserved.assert_called_once_with(False)


def test_the_threshold_clears_the_floor_the_reservation_produces():
    """Shedding must not fight the width it enables: the threshold has to sit
    above the sidebar minimum a reserved button produces (~150px)."""
    mod = importlib.import_module('sshpilot.window')
    assert mod._ROW_ACTIONS_MIN_WIDTH > 150


def test_repeat_calls_do_not_walk_the_list_again():
    win, mod, rows = _window(250)
    mod.MainWindow._apply_sidebar_row_actions(win)
    mod.MainWindow._apply_sidebar_row_actions(win)
    for r in rows:
        assert r.set_actions_reserved.call_count == 1
        assert r.set_indicators_reserved.call_count == 1
    mod.MainWindow._apply_sidebar_row_actions(win, force=True)
    for r in rows:
        assert r.set_actions_reserved.call_count == 2
        assert r.set_indicators_reserved.call_count == 2


def _connection_row_indicators():
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row._compact = False
    row._indicators_reserved = True
    row.config = SimpleNamespace(get_setting=lambda key, default=None: default)
    row.connection = SimpleNamespace(forwarding_rules=())
    row.indicator_box = MagicMock(name='indicator_box')
    row.indicator_box.get_first_child.return_value = None
    row._refresh_row_tooltip = MagicMock()
    return row, mod


def test_narrow_sidebar_sheds_port_forwarding_indicators():
    row, mod = _connection_row_indicators()
    row._update_forwarding_indicators = MagicMock()

    mod.ConnectionRow.set_indicators_reserved(row, False)

    assert row._indicators_reserved is False
    row.indicator_box.set_visible.assert_called_with(False)
    row._update_forwarding_indicators.assert_not_called()


def test_widening_restores_port_forwarding_indicators():
    row, mod = _connection_row_indicators()
    row._indicators_reserved = False
    row._update_forwarding_indicators = MagicMock()

    mod.ConnectionRow.set_indicators_reserved(row, True)

    assert row._indicators_reserved is True
    row.indicator_box.set_visible.assert_called_with(True)
    row._update_forwarding_indicators.assert_called_once()


def test_shed_port_forwarding_indicators_ignored_while_compact():
    row, mod = _connection_row_indicators()
    row._compact = True
    row._update_forwarding_indicators = MagicMock()

    mod.ConnectionRow.set_indicators_reserved(row, False)

    assert row._indicators_reserved is False
    row.indicator_box.set_visible.assert_not_called()
    row._update_forwarding_indicators.assert_not_called()


def test_update_forwarding_indicators_noops_when_shed():
    row, mod = _connection_row_indicators()
    row._indicators_reserved = False

    mod.ConnectionRow._update_forwarding_indicators(row)

    row.indicator_box.set_visible.assert_called_with(False)
    row.indicator_box.append.assert_not_called()
