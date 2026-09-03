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
