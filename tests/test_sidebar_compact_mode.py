"""Compact sidebar mode forces title-only flat rows."""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _window(mode='compact', **pref_overrides):
    win_mod = importlib.import_module('sshpilot.window')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)
    prefs = {
        'ui.sidebar_mode': mode,
        'ui.sidebar_show_user_hostname': True,
        'ui.sidebar_show_group_count': True,
        'ui.sidebar_show_connection_status': True,
        'ui.sidebar_show_connection_icon': True,
        'ui.sidebar_show_group_icon': True,
        'ui.sidebar_flat_rows': False,
    }
    prefs.update(pref_overrides)
    win.config = MagicMock()
    win.config.get_setting.side_effect = lambda key, default=None: prefs.get(
        key, default
    )

    conn = SimpleNamespace(
        connection_icon=MagicMock(name='connection_icon'),
        host_label=MagicMock(name='host_label'),
        status_icon=MagicMock(name='status_icon'),
        update_status=MagicMock(name='update_status'),
        _update_forwarding_indicators=MagicMock(name='fwd'),
        apply_row_style=MagicMock(name='apply_row_style'),
        apply_sidebar_mode=MagicMock(name='apply_sidebar_mode'),
        _reveal_file_manager_button=MagicMock(name='reveal_fm'),
        _pointer_is_on_row=MagicMock(return_value=False),
    )
    group = SimpleNamespace(
        count_label=MagicMock(name='count_label'),
        group_id='g1',
        icon=MagicMock(name='group_icon'),
        expand_button=MagicMock(name='expand_button'),
        apply_row_style=MagicMock(name='apply_row_style'),
        apply_sidebar_mode=MagicMock(name='apply_sidebar_mode'),
        _reveal_row_actions=MagicMock(name='reveal_actions'),
        _pointer_is_on_row=MagicMock(return_value=False),
    )
    conn.get_next_sibling = lambda: group
    group.get_next_sibling = lambda: None
    lb = MagicMock(name='connection_list')
    lb.get_first_child.return_value = conn
    win.connection_list = lb
    win.connection_manager = MagicMock()
    win.connection_manager.get_connections.return_value = []
    win._attach_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._refresh_sidebar_forwarding_rules = lambda *_a, **_k: None
    return win, win_mod.MainWindow, conn, group


def test_compact_mode_hides_chrome_and_forces_flat():
    win, cls, conn, group = _window(mode='compact')
    cls.update_sidebar_display(win)

    conn.connection_icon.set_visible.assert_called_with(False)
    conn.host_label.set_visible.assert_called_with(False)
    group.count_label.set_visible.assert_called_with(False)
    group.icon.set_visible.assert_called_with(False)
    group.expand_button.set_visible.assert_called_with(True)
    conn.apply_row_style.assert_called_with(True)
    group.apply_row_style.assert_called_with(True)
    conn.apply_sidebar_mode.assert_called_once()
    group.apply_sidebar_mode.assert_called_once()


def test_full_mode_honors_individual_prefs():
    win, cls, conn, group = _window(mode='full')
    cls.update_sidebar_display(win)

    conn.connection_icon.set_visible.assert_called_with(True)
    conn.host_label.set_visible.assert_called_with(True)
    group.count_label.set_visible.assert_called_with(True)
    group.icon.set_visible.assert_called_with(True)
    group.expand_button.set_visible.assert_called_with(True)
    conn.apply_row_style.assert_called_with(False)


def test_sidebar_is_compact_helper():
    mod = importlib.import_module('sshpilot.sidebar')
    assert mod._sidebar_is_compact(None) is False
    cfg = MagicMock()
    cfg.get_setting.return_value = 'compact'
    assert mod._sidebar_is_compact(cfg) is True
    cfg.get_setting.return_value = 'full'
    assert mod._sidebar_is_compact(cfg) is False


def test_title_centering_single_line_fills_and_centers():
    mod = importlib.import_module('sshpilot.sidebar')
    info = MagicMock(name='info_box')
    title = MagicMock(name='title_label')

    mod._apply_sidebar_title_centering(info, title, single_line=True)
    info.set_valign.assert_called_with(mod.Gtk.Align.FILL)
    title.set_vexpand.assert_called_with(True)
    title.set_valign.assert_called_with(mod.Gtk.Align.CENTER)

    mod._apply_sidebar_title_centering(info, title, single_line=False)
    info.set_valign.assert_called_with(mod.Gtk.Align.CENTER)
    title.set_vexpand.assert_called_with(False)
    title.set_valign.assert_called_with(mod.Gtk.Align.START)


def test_compact_forces_flat_rows_helper():
    mod = importlib.import_module('sshpilot.sidebar')
    cfg = MagicMock()

    def _get(key, default=None):
        if key == 'ui.sidebar_mode':
            return 'compact'
        if key == 'ui.sidebar_flat_rows':
            return False
        return default

    cfg.get_setting.side_effect = _get
    assert mod._use_flat_sidebar_rows(cfg) is True


def test_compact_density_is_comfortable():
    mod = importlib.import_module('sshpilot.sidebar')
    assert mod._SIDEBAR_ROW_DENSITY_COMPACT == (4, 4, 0, 0, 4)
    assert mod._SIDEBAR_ROW_DENSITY_FULL == (12, 12, 6, 6, 12)


def test_sidebar_list_compact_class_toggles():
    mod = importlib.import_module('sshpilot.sidebar')
    lb = MagicMock(name='connection_list')
    lb.has_css_class.return_value = False
    cfg = MagicMock()
    cfg.get_setting.return_value = 'compact'
    mod._apply_sidebar_list_compact_class(lb, cfg)
    lb.add_css_class.assert_called_with('sidebar-compact')

    lb.has_css_class.return_value = True
    cfg.get_setting.return_value = 'full'
    mod._apply_sidebar_list_compact_class(lb, cfg)
    lb.remove_css_class.assert_called_with('sidebar-compact')
