"""PTY-less VTE must defer feed() while the user is selecting (Ptyxis parity).

VTE pauses its PTY reader during drag-select. Daemon tabs push with feed(),
so without a buffer htop redraws call deselect_all mid-drag.
"""

from types import SimpleNamespace

from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_display_pause import (
    DeferredDisplayFeed,
    selection_press_owns_pointer,
)
from sshpilot.terminal_input import MouseTrackingState


def test_selection_press_owns_pointer_without_mouse_tracking():
    assert selection_press_owns_pointer(
        mouse_tracking_active=False, shift_held=False
    )
    assert selection_press_owns_pointer(
        mouse_tracking_active=False, shift_held=True
    )


def test_selection_press_requires_shift_under_mouse_tracking():
    assert not selection_press_owns_pointer(
        mouse_tracking_active=True, shift_held=False
    )
    assert selection_press_owns_pointer(
        mouse_tracking_active=True, shift_held=True
    )


def test_deferred_display_feed_buffers_then_flushes_in_order():
    pause = DeferredDisplayFeed()
    assert pause.accept(b"live") == b"live"

    pause.begin()
    assert pause.accept(b"one") is None
    assert pause.accept(b"two") is None
    assert pause.buffered_length == 6

    assert pause.end() == b"onetwo"
    assert pause.paused is False
    assert pause.accept(b"live-again") == b"live-again"


def test_deferred_display_feed_reset_drops_buffer():
    pause = DeferredDisplayFeed()
    pause.begin()
    pause.accept(b"gone")
    pause.reset()
    assert pause.paused is False
    assert pause.buffered_length == 0
    assert pause.end() == b""


def test_feed_display_defers_while_selection_pause_is_active():
    widget = TerminalWidget.__new__(TerminalWidget)
    feeds = []
    widget.backend = SimpleNamespace(feed=feeds.append)
    widget._mouse_tracking = MouseTrackingState()
    widget._display_feed_pause = DeferredDisplayFeed()

    widget._feed_display(b"before")
    widget._display_feed_pause.begin()
    widget._feed_display(b"\x1b[Hhtop-redraw")
    widget._feed_display(b"-more")
    assert feeds == [b"before"]

    widget._resume_selection_display_feed()
    assert feeds == [b"before", b"\x1b[Hhtop-redraw-more"]
    assert widget._display_feed_pause.paused is False


def test_feed_display_still_logs_mouse_tracking_after_flush(caplog):
    import logging

    widget = TerminalWidget.__new__(TerminalWidget)
    feeds = []
    widget.backend = SimpleNamespace(feed=feeds.append)
    widget._mouse_tracking = MouseTrackingState()
    widget._display_feed_pause = DeferredDisplayFeed()

    widget._display_feed_pause.begin()
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        widget._feed_display(b"\x1b[?1000h")
    assert feeds == []
    assert not any(
        "mouse tracking changed" in record.getMessage()
        for record in caplog.records
    )

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        widget._resume_selection_display_feed()
    assert feeds == [b"\x1b[?1000h"]
    assert any(
        "modes=[1000]" in record.getMessage() for record in caplog.records
    )


# --- SelectionFeedPauseController -------------------------------------------
#
# The controller is what ended the first attempt's permanent freeze: VTE
# claims the pointer sequence for its own drag-select, so a Gtk.GestureClick
# on the same widget only ever sees "pressed" then "cancel" -- the "released"
# half that would have un-paused the feed never arrives.

import pytest

from sshpilot.terminal_display_pause import SelectionFeedPauseController


@pytest.fixture
def gdk():
    """Real GDK constants, or integer stand-ins under the stubbed ``gi``."""
    from gi.repository import Gdk

    if not isinstance(Gdk.BUTTON_PRIMARY, int):
        Gdk.BUTTON_PRIMARY = 1
        Gdk.BUTTON_SECONDARY = 3
        Gdk.ModifierType.SHIFT_MASK = 1
    return Gdk


class _FakeEvent:
    def __init__(self, event_type, button=1, modifiers=0):
        self._type = event_type
        self._button = button
        self._modifiers = modifiers

    def get_event_type(self):
        return self._type

    def get_button(self):
        return self._button

    def get_modifier_state(self):
        return self._modifiers


class _FakeWidget:
    def __init__(self):
        self.controllers = []

    def add_controller(self, controller):
        self.controllers.append(controller)

    def remove_controller(self, controller):
        self.controllers.remove(controller)


class _RecordingController:
    def __init__(self):
        self.phase = None
        self.handlers = {}

    def set_propagation_phase(self, phase):
        self.phase = phase

    def connect(self, signal, callback):
        self.handlers[signal] = callback


def _observer(mouse_tracking=False):
    pause = DeferredDisplayFeed()
    painted = []

    def flush():
        deferred = pause.end()
        if deferred:
            painted.append(deferred)

    observer = SelectionFeedPauseController(
        pause,
        mouse_tracking_active=lambda: mouse_tracking,
        flush=flush,
    )
    return observer, pause, painted


def test_observer_is_a_legacy_controller_never_a_gesture(monkeypatch):
    """A gesture would compete with VTE's drag and never see the release."""
    from gi.repository import Gtk

    created = []

    def _factory():
        controller = _RecordingController()
        created.append(controller)
        return controller

    monkeypatch.setattr(Gtk, "EventControllerLegacy", _factory, raising=False)

    def _no_gestures(*_a, **_k):
        raise AssertionError("selection feed pause must not install a gesture")

    monkeypatch.setattr(Gtk, "GestureClick", _no_gestures, raising=False)

    observer, _pause, _painted = _observer()
    widget = _FakeWidget()
    assert observer.install(widget) is True
    (controller,) = created
    assert widget.controllers == [controller]
    assert controller.phase == Gtk.PropagationPhase.CAPTURE
    assert "event" in controller.handlers

    observer.uninstall()
    assert widget.controllers == []


def test_observer_pauses_on_press_and_resumes_on_release(gdk):
    observer, pause, painted = _observer()

    assert observer._on_event(None, _FakeEvent(gdk.EventType.BUTTON_PRESS)) is False
    assert pause.paused is True
    assert pause.accept(b"htop-redraw") is None

    assert observer._on_event(None, _FakeEvent(gdk.EventType.BUTTON_RELEASE)) is False
    assert pause.paused is False
    assert painted == [b"htop-redraw"]


def test_observer_leaves_plain_click_alone_under_mouse_tracking(gdk):
    """htop owns the pointer: the click is its own, and the feed must run."""
    observer, pause, _painted = _observer(mouse_tracking=True)

    observer._on_event(None, _FakeEvent(gdk.EventType.BUTTON_PRESS))
    assert pause.paused is False

    observer._on_event(
        None,
        _FakeEvent(gdk.EventType.BUTTON_PRESS, modifiers=gdk.ModifierType.SHIFT_MASK),
    )
    assert pause.paused is True


def test_observer_ignores_non_primary_buttons(gdk):
    observer, pause, _painted = _observer()
    observer._on_event(
        None, _FakeEvent(gdk.EventType.BUTTON_PRESS, button=gdk.BUTTON_SECONDARY)
    )
    assert pause.paused is False


def test_observer_resumes_when_the_grab_is_broken(gdk):
    """A stolen pointer grab still has to end the pause, never freeze."""
    observer, pause, painted = _observer()
    observer._on_event(None, _FakeEvent(gdk.EventType.BUTTON_PRESS))
    pause.accept(b"deferred")
    observer._on_event(None, _FakeEvent(gdk.EventType.GRAB_BROKEN))
    assert pause.paused is False
    assert painted == [b"deferred"]


def test_observer_resumes_when_an_event_cannot_be_read(gdk):
    """Even a malformed event must not leave the display buffered forever."""
    observer, pause, painted = _observer()
    observer._on_event(None, _FakeEvent(gdk.EventType.BUTTON_PRESS))
    pause.accept(b"deferred")

    class _Broken:
        def get_event_type(self):
            raise RuntimeError("boom")

    assert observer._on_event(None, _Broken()) is False
    assert pause.paused is False
    assert painted == [b"deferred"]
