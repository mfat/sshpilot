"""Scroll speed and scroll units (issue #1269).

VTE's built-in fallback scrolling is unit-blind: it multiplies whatever delta it
is handed by ``max(1, ceil(rows/10))`` *lines*.  A wheel notch arrives as
``dy == 1`` so that works out, but GDK reports a touchpad -- and on macOS every
precise device, including the Magic Mouse -- as ``GDK_SCROLL_UNIT_SURFACE`` with
``dy`` a raw pixel count.  Multiplying those pixels by a line count is how a
small two-finger flick turned into a hundred lines.  We turn the fallback off
and make the distinction ourselves.
"""

import types
import pytest

pytest.importorskip("gi")

from gi.repository import Gdk

from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import VTETerminalBackend

CELL_HEIGHT = 20


def _terminal(scroll_lines=None):
    widget = TerminalWidget.__new__(TerminalWidget)
    if scroll_lines is None:
        widget.config = None
    else:
        widget.config = types.SimpleNamespace(
            get_setting=lambda key, default=None: (
                scroll_lines if key == 'terminal.scroll_lines' else default
            )
        )
    return widget


def _vte(in_pixels=True, cell_height=CELL_HEIGHT):
    return types.SimpleNamespace(
        get_char_height=lambda: cell_height,
        get_scroll_unit_is_pixels=lambda: in_pixels,
    )


def _controller(unit):
    return types.SimpleNamespace(
        get_unit=lambda: unit,
    )


def test_touchpad_pixels_move_the_view_one_to_one():
    """The macOS complaint.  A precise device's dy is a distance, not a count:
    30pt of finger travel must move 30pt of content, as it does in every native
    macOS application.  VTE's fallback made that 30 * ceil(rows/10) lines.
    """
    terminal = _terminal()
    delta = terminal._history_scroll_delta(
        _vte(), _controller(Gdk.ScrollUnit.SURFACE), 30.0
    )
    assert delta == 30.0


def test_a_wheel_notch_moves_a_fixed_number_of_lines():
    """Fixed, unlike VTE's ceil(rows/10), which is ~4 lines in a small window
    and ~7 maximised -- the inconsistency is most of why it reads as fast.
    """
    terminal = _terminal()
    delta = terminal._history_scroll_delta(
        _vte(), _controller(Gdk.ScrollUnit.WHEEL), 1.0
    )
    assert delta == 3 * CELL_HEIGHT


def test_wheel_step_is_configurable():
    terminal = _terminal(scroll_lines=5)
    delta = terminal._history_scroll_delta(
        _vte(), _controller(Gdk.ScrollUnit.WHEEL), 1.0
    )
    assert delta == 5 * CELL_HEIGHT


@pytest.mark.parametrize(
    "value,expected", [(0, 1), (-4, 1), (999, 20), ("nonsense", 3)]
)
def test_wheel_step_survives_a_hostile_setting(value, expected):
    """A zero or negative step would freeze scrolling outright."""
    assert _terminal(scroll_lines=value)._wheel_scroll_lines() == expected


def test_line_valued_adjustments_are_converted():
    """set_scroll_unit_is_pixels() is VTE >= 0.66 and configure() applies it
    best-effort, so the adjustment may still be counting lines.  Ask, do not
    assume -- guessing wrong here is a 20x error in either direction.
    """
    terminal = _terminal()
    surface = terminal._history_scroll_delta(
        _vte(in_pixels=False), _controller(Gdk.ScrollUnit.SURFACE), 30.0
    )
    wheel = terminal._history_scroll_delta(
        _vte(in_pixels=False), _controller(Gdk.ScrollUnit.WHEEL), 1.0
    )
    assert surface == 30.0 / CELL_HEIGHT
    assert wheel == 3


def test_direction_is_preserved():
    terminal = _terminal()
    assert (
        terminal._history_scroll_delta(
            _vte(), _controller(Gdk.ScrollUnit.SURFACE), -30.0
        )
        == -30.0
    )
    assert (
        terminal._history_scroll_delta(
            _vte(), _controller(Gdk.ScrollUnit.WHEEL), -1.0
        )
        == -3 * CELL_HEIGHT
    )


def test_an_unmeasurable_cell_scrolls_nothing():
    """get_char_height() is 0 before the widget is allocated; dividing by it
    would raise inside a GTK signal handler.
    """
    terminal = _terminal()
    assert (
        terminal._history_scroll_delta(
            _vte(cell_height=0), _controller(Gdk.ScrollUnit.WHEEL), 1.0
        )
        == 0.0
    )


def test_a_controller_without_a_unit_is_treated_as_a_wheel():
    """Every X11 scroll event is GDK_SCROLL_UNIT_WHEEL anyway, so a missing or
    broken unit must not be mistaken for a pixel delta.
    """
    terminal = _terminal()
    controller = types.SimpleNamespace(
        get_unit=lambda: (_ for _ in ()).throw(RuntimeError("no unit")),
    )
    assert terminal._history_scroll_delta(_vte(), controller, 1.0) == 3 * CELL_HEIGHT


def test_non_scrollable_backends_are_left_alone():
    """PyXterm is a WebKit view: it scrolls inside the page and exposes no
    adjustment to drive.
    """
    terminal = _terminal()
    terminal.terminal_widget = object()
    assert (
        terminal._on_history_scroll(_controller(Gdk.ScrollUnit.WHEEL), 0.0, 1.0)
        is False
    )


def test_vte_backend_hands_scrolling_over_to_us():
    """The two flags the fix turns on.  Leaving fallback scrolling enabled would
    mean VTE consumes the event and our unit-aware controller never runs.
    """
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    calls = {}
    backend.vte = types.SimpleNamespace(
        set_enable_fallback_scrolling=lambda v: calls.__setitem__("fallback", v),
        set_scroll_unit_is_pixels=lambda v: calls.__setitem__("pixels", v),
    )
    backend.configure()
    assert calls == {"fallback": False, "pixels": True}
