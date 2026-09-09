"""Hostname / group-count stay off during strip↔full transitions."""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _window(host_pref=True, count_pref=True):
    win_mod = importlib.import_module('sshpilot.window')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)
    win._sidebar_minimal = False
    win._sidebar_suppress_secondary_labels = False
    win.config = MagicMock()
    win.config.get_setting.side_effect = lambda key, default=None: {
        'ui.sidebar_show_user_hostname': host_pref,
        'ui.sidebar_show_group_count': count_pref,
    }.get(key, default)

    conn = SimpleNamespace(
        host_label=MagicMock(name='host_label'),
        _compact=False,
    )
    group = SimpleNamespace(
        count_label=MagicMock(name='count_label'),
        _compact=False,
    )
    conn.get_next_sibling = lambda: group
    group.get_next_sibling = lambda: None
    lb = MagicMock(name='connection_list')
    lb.get_first_child.return_value = conn
    win.connection_list = lb
    return win, win_mod.MainWindow, conn, group


def test_suppress_hides_hostname_and_group_count():
    win, cls, conn, group = _window()
    cls._set_sidebar_secondary_labels_suppressed(win, True)
    assert win._sidebar_suppress_secondary_labels is True
    conn.host_label.set_visible.assert_called_with(False)
    group.count_label.set_visible.assert_called_with(False)


def test_unsuppress_restores_preferences():
    win, cls, conn, group = _window(host_pref=True, count_pref=True)
    cls._set_sidebar_secondary_labels_suppressed(win, True)
    conn.host_label.reset_mock()
    group.count_label.reset_mock()

    cls._set_sidebar_secondary_labels_suppressed(win, False)
    assert win._sidebar_suppress_secondary_labels is False
    conn.host_label.set_visible.assert_called_with(True)
    group.count_label.set_visible.assert_called_with(True)


def test_unsuppress_respects_prefs_off():
    win, cls, conn, group = _window(host_pref=False, count_pref=False)
    cls._set_sidebar_secondary_labels_suppressed(win, False)
    conn.host_label.set_visible.assert_called_with(False)
    group.count_label.set_visible.assert_called_with(False)


def test_compact_rows_stay_hidden_when_unsuppressed():
    win, cls, conn, group = _window()
    conn._compact = True
    group._compact = True
    cls._set_sidebar_secondary_labels_suppressed(win, False)
    conn.host_label.set_visible.assert_called_with(False)
    group.count_label.set_visible.assert_called_with(False)


def test_divider_drag_does_not_suppress_secondary_labels(monkeypatch):
    """Live divider drag pauses tips only — hostname/count stay put.

    Dragging no longer collapses into the strip, so dropping secondary lines
    mid-resize only made rows flicker. Mode transitions still suppress.
    """
    win, cls, conn, group = _window()
    win._sidebar_minimal = False
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
    assert win._sidebar_suppress_secondary_labels is False
    conn.host_label.set_visible.assert_not_called()
    group.count_label.set_visible.assert_not_called()
    assert scheduled == ['restore']
    assert tips == [True]


def test_divider_drag_in_strip_does_not_unsuppress(monkeypatch):
    win, cls, conn, group = _window()
    win._sidebar_minimal = True
    win._sidebar_suppress_secondary_labels = True
    win._sidebar_density_restore_source = 0
    win._sidebar_width_animation = None
    queued = []
    conn.host_label.reset_mock()

    monkeypatch.setattr(
        cls, '_queue_tips_banner_restore',
        lambda self: queued.append('tips'))

    # Still minimal after drag settle — secondary labels stay off; tips queued.
    cls._restore_sidebar_density_after_drag(win)
    conn.host_label.set_visible.assert_not_called()
    assert win._sidebar_suppress_secondary_labels is True
    assert queued == ['tips']
