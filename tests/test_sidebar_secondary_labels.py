"""Hostname / group-count visibility follows sidebar display preferences."""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _window(host_pref=True, count_pref=True):
    win_mod = importlib.import_module('sshpilot.window')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)
    win.config = MagicMock()
    win.config.get_setting.side_effect = lambda key, default=None: {
        'ui.sidebar_mode': 'full',
        'ui.sidebar_show_user_hostname': host_pref,
        'ui.sidebar_show_group_count': count_pref,
        'ui.sidebar_show_connection_status': True,
        'ui.sidebar_show_connection_icon': True,
        'ui.sidebar_show_group_icon': True,
        'ui.sidebar_flat_rows': False,
    }.get(key, default)

    conn = SimpleNamespace(
        host_label=MagicMock(name='host_label'),
        connection_icon=MagicMock(name='connection_icon'),
        status_icon=MagicMock(name='status_icon'),
        update_status=MagicMock(name='update_status'),
    )
    group = SimpleNamespace(
        count_label=MagicMock(name='count_label'),
        group_id='g1',
        icon=MagicMock(name='group_icon'),
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


def test_update_sidebar_display_shows_secondary_labels_from_prefs():
    win, cls, conn, group = _window(host_pref=True, count_pref=True)
    cls.update_sidebar_display(win)
    conn.host_label.set_visible.assert_called_with(True)
    group.count_label.set_visible.assert_called_with(True)


def test_update_sidebar_display_hides_secondary_labels_when_prefs_off():
    win, cls, conn, group = _window(host_pref=False, count_pref=False)
    cls.update_sidebar_display(win)
    conn.host_label.set_visible.assert_called_with(False)
    group.count_label.set_visible.assert_called_with(False)


def test_divider_drag_does_not_change_secondary_labels(monkeypatch):
    """Live divider drag pauses tips only — hostname/count stay put."""
    win, cls, conn, group = _window()
    win._sidebar_density_restore_source = 0
    scheduled = []
    tips = []

    monkeypatch.setattr(
        cls, '_schedule_sidebar_density_restore',
        lambda self: scheduled.append('restore'))
    monkeypatch.setattr(
        cls, '_pause_tips_banner_for_sidebar_anim',
        lambda self, pause: tips.append(pause))

    cls._on_sidebar_divider_drag(win, 180)
    conn.host_label.set_visible.assert_not_called()
    group.count_label.set_visible.assert_not_called()
    assert scheduled == ['restore']
    assert tips == [True]


def test_density_restore_after_drag_only_queues_tips(monkeypatch):
    win, cls, conn, group = _window()
    win._sidebar_density_restore_source = 0
    queued = []

    monkeypatch.setattr(
        cls, '_queue_tips_banner_restore',
        lambda self: queued.append('tips'))

    result = cls._restore_sidebar_density_after_drag(win)
    conn.host_label.set_visible.assert_not_called()
    assert queued == ['tips']
    from gi.repository import GLib
    assert result is GLib.SOURCE_REMOVE
