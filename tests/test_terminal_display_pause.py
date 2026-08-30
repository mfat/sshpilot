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
