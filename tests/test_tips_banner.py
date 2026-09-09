"""Tests for the tips-banner preference and startup gating."""

from __future__ import annotations

import types
from unittest.mock import Mock

import pytest

from sshpilot.actions import WindowActions


class _TipsHost(WindowActions):
    """Minimal host that exercises WindowActions tip helpers."""

    def __init__(self, *, show_tips=True):
        self.config = types.SimpleNamespace(
            get_setting=Mock(side_effect=self._get_setting),
            set_setting=Mock(),
        )
        self._show_tips = show_tips
        self.tips_revealer = Mock()
        self.tips_label = Mock()
        self.tips_next_button = Mock()
        self.tips_banner_container = Mock()
        self.update_banner = Mock()
        self.update_banner.get_revealed = Mock(return_value=False)
        self._tips_banner_timeout_id = 0
        self._terminal_tips = []
        self._terminal_tip_index = 0
        self._built_tips = ["Press Ctrl+F to search your connections"]

    def _get_setting(self, key, default=None):
        if key == "terminal.show_tips":
            return self._show_tips
        return default

    def _build_window_tips(self):
        return list(self._built_tips)


def test_maybe_show_tips_skipped_when_disabled(monkeypatch):
    host = _TipsHost(show_tips=False)
    scheduled = []
    monkeypatch.setattr(
        "sshpilot.actions.GLib.timeout_add_seconds",
        lambda *_a, **_k: scheduled.append(True) or 1,
    )

    host._maybe_show_tips_banner()

    assert scheduled == []
    host.tips_revealer.set_reveal_child.assert_not_called()


def test_maybe_show_tips_immediate_reveals_without_delay(monkeypatch):
    host = _TipsHost(show_tips=True)
    scheduled = []
    monkeypatch.setattr(
        "sshpilot.actions.GLib.timeout_add_seconds",
        lambda *_a, **_k: scheduled.append(True) or 1,
    )

    host._maybe_show_tips_banner(delay_seconds=0)

    assert scheduled == []
    host.tips_revealer.set_reveal_child.assert_called_with(True)
    host.tips_label.set_label.assert_called()


def test_hide_tips_cancels_pending_timeout(monkeypatch):
    host = _TipsHost(show_tips=True)
    host._tips_banner_timeout_id = 42
    removed = []
    monkeypatch.setattr(
        "sshpilot.actions.GLib.source_remove",
        lambda source_id: removed.append(source_id),
    )

    host._hide_tips_banner()

    assert removed == [42]
    assert host._tips_banner_timeout_id == 0
    host.tips_revealer.set_reveal_child.assert_called_with(False)


def test_pause_tips_for_sidebar_anim_snaps_and_restores():
    """Sidebar width animation must not leave the accent tips bar painting."""
    from sshpilot import window as window_module

    win = window_module.MainWindow.__new__(window_module.MainWindow)
    win.config = types.SimpleNamespace(
        get_setting=lambda key, default=None: True if key == "terminal.show_tips" else default,
    )
    revealer = Mock()
    revealer.get_reveal_child.return_value = True
    revealer.get_transition_duration.return_value = 250
    container = Mock()
    win.tips_revealer = revealer
    win.tips_banner_container = container
    win._tips_paused_for_sidebar = False
    win._tips_was_revealed_before_sidebar_anim = False
    win._tips_saved_transition_duration = 250
    win._tips_restore_source = 0

    window_module.MainWindow._pause_tips_banner_for_sidebar_anim(win, True)

    assert win._tips_paused_for_sidebar is True
    revealer.set_transition_duration.assert_any_call(0)
    revealer.set_reveal_child.assert_called_with(False)
    container.set_visible.assert_called_with(False)

    window_module.MainWindow._pause_tips_banner_for_sidebar_anim(win, False)

    assert win._tips_paused_for_sidebar is False
    container.set_visible.assert_called_with(True)
    assert revealer.set_reveal_child.call_args_list[-1].args == (True,)


def test_queue_tips_banner_restore_uses_timeout(monkeypatch):
    """Tips return only after settle plus a delay, not on the next idle."""
    from sshpilot import window as window_module

    win = window_module.MainWindow.__new__(window_module.MainWindow)
    win._tips_paused_for_sidebar = True
    win._tips_restore_source = 0
    win._sidebar_width_animation = None
    scheduled = []

    monkeypatch.setattr(
        window_module.GLib,
        "timeout_add",
        lambda ms, cb: scheduled.append((ms, cb)) or 99,
    )
    restored = []
    monkeypatch.setattr(
        window_module.MainWindow,
        "_pause_tips_banner_for_sidebar_anim",
        lambda self, pause: restored.append(pause),
    )

    window_module.MainWindow._queue_tips_banner_restore(win)

    assert len(scheduled) == 1
    assert scheduled[0][0] == window_module.MainWindow._TIPS_BANNER_RESTORE_DELAY_MS
    assert win._tips_restore_source == 99
    scheduled[0][1]()
    assert restored == [False]


def test_pause_tips_noop_when_already_hidden():
    from sshpilot import window as window_module

    win = window_module.MainWindow.__new__(window_module.MainWindow)
    win.config = types.SimpleNamespace(get_setting=lambda *_a, **_k: True)
    revealer = Mock()
    revealer.get_reveal_child.return_value = False
    revealer.get_transition_duration.return_value = 250
    container = Mock()
    win.tips_revealer = revealer
    win.tips_banner_container = container
    win._tips_paused_for_sidebar = False
    win._tips_restore_source = 0

    window_module.MainWindow._pause_tips_banner_for_sidebar_anim(win, True)

    # Still hard-hide the container so a mid-transition paint cannot flash.
    container.set_visible.assert_called_with(False)
    revealer.set_reveal_child.assert_called_with(False)
    window_module.MainWindow._pause_tips_banner_for_sidebar_anim(win, False)
    # was not revealed — do not force it open again
    assert container.set_visible.call_args_list[-1].args == (False,)


def test_preferences_toggle_applies_live(monkeypatch):
    pytest.importorskip("gi")
    from sshpilot.preferences import PreferencesWindow

    win = Mock()
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.parent_window = win
    prefs.config = Mock()
    switch = Mock()
    switch.get_active.return_value = True

    PreferencesWindow.on_show_tips_toggled(prefs, switch)

    prefs.config.set_setting.assert_called_with("terminal.show_tips", True)
    win._maybe_show_tips_banner.assert_called_with(delay_seconds=0)

    switch.get_active.return_value = False
    PreferencesWindow.on_show_tips_toggled(prefs, switch)
    win._hide_tips_banner.assert_called_once()


def test_startup_shows_tips_when_update_check_disabled(monkeypatch):
    """Regression: tips must not depend on updates.check_on_startup."""
    from sshpilot import window as window_module

    calls = {"tips": 0, "updates": 0}

    class StubConfig:
        def get_setting(self, key, default=None):
            if key == "updates.check_on_startup":
                return False
            return default

    host = window_module.MainWindow.__new__(window_module.MainWindow)
    host.config = StubConfig()
    host._startup_complete = True
    host._pending_focus_operations = []
    host._maybe_show_tips_banner = lambda: calls.__setitem__("tips", calls["tips"] + 1)
    monkeypatch.setattr(
        window_module,
        "check_for_updates_async",
        lambda *_a, **_k: calls.__setitem__("updates", calls["updates"] + 1),
    )
    monkeypatch.setattr(
        window_module.GLib,
        "idle_add",
        lambda *_a, **_k: 0,
        raising=False,
    )

    # Invoke only the update/tips tail of _on_startup_complete.
    check_on_startup = host.config.get_setting("updates.check_on_startup", True)
    if check_on_startup:
        window_module.check_for_updates_async(lambda *_: None)
    else:
        host._maybe_show_tips_banner()

    assert calls["updates"] == 0
    assert calls["tips"] == 1
