"""The macOS click guard drops the stale button-less motion before a release."""

from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk  # noqa: E402

import pytest  # noqa: E402

from sshpilot import macos_click_guard  # noqa: E402
from sshpilot.macos_click_guard import (  # noqa: E402
    MAX_STALE_MOTIONS_PER_PRESS,
    _ClickGuard,
)

# The test session may run against stubbed GI types, so masks are plain ints.
BUTTON1 = 1 << 8
BUTTON3 = 1 << 10
NO_BUTTONS = 0


@pytest.fixture(autouse=True)
def _int_masks(monkeypatch):
    monkeypatch.setattr(
        macos_click_guard, "_BUTTON_MASKS", {1: BUTTON1, 3: BUTTON3})


def _event(event_type, *, button=1, state=NO_BUTTONS):
    return SimpleNamespace(
        get_event_type=lambda: event_type,
        get_button=lambda: button,
        get_modifier_state=lambda: state,
    )


def _feed(guard, event):
    controller = SimpleNamespace(get_current_event=lambda: event)
    return guard.on_event(controller, None)


def _press(guard, button=1):
    return _feed(guard, _event(Gdk.EventType.BUTTON_PRESS, button=button))


def _motion(guard, state):
    return _feed(guard, _event(Gdk.EventType.MOTION_NOTIFY, state=state))


def _release(guard):
    return _feed(guard, _event(Gdk.EventType.BUTTON_RELEASE))


def test_stale_motion_during_press_is_swallowed():
    guard = _ClickGuard()
    assert _press(guard) is False
    assert _motion(guard, BUTTON1) is False
    assert _motion(guard, NO_BUTTONS) is True
    assert _release(guard) is False


def test_motion_outside_a_press_is_left_alone():
    guard = _ClickGuard()
    assert _motion(guard, NO_BUTTONS) is False
    _press(guard)
    _release(guard)
    assert _motion(guard, NO_BUTTONS) is False


def test_mask_follows_the_pressed_button():
    guard = _ClickGuard()
    _press(guard, button=3)
    assert _motion(guard, BUTTON1) is True
    assert _motion(guard, BUTTON3) is False


def test_a_really_lost_release_is_handed_back_to_gtk():
    guard = _ClickGuard()
    _press(guard)
    for _ in range(MAX_STALE_MOTIONS_PER_PRESS):
        assert _motion(guard, NO_BUTTONS) is True
    assert _motion(guard, NO_BUTTONS) is False
    assert _motion(guard, NO_BUTTONS) is False
