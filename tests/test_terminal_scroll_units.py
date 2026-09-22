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

from sshpilot import terminal as terminal_mod
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import VTETerminalBackend

CELL_HEIGHT = 20


def _terminal():
    return TerminalWidget.__new__(TerminalWidget)


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
    assert delta == 1 * CELL_HEIGHT


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
    assert wheel == 1


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
        == -1 * CELL_HEIGHT
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
    assert terminal._history_scroll_delta(_vte(), controller, 1.0) == 1 * CELL_HEIGHT


def test_a_gtk_without_scroll_units_at_all_is_treated_as_a_wheel(monkeypatch):
    """get_unit() is GTK >= 4.8 and the packaging asks only for 4.6.  There
    Gdk.ScrollUnit is missing too, so the SURFACE comparison used to be
    ``None == None`` and a wheel notch scrolled a single pixel.  The test above
    cannot catch that: Gdk.ScrollUnit exists in the test environment, so it
    never reaches this branch.
    """
    monkeypatch.delattr(Gdk, "ScrollUnit", raising=False)
    terminal = _terminal()
    controller = types.SimpleNamespace(
        get_unit=lambda: (_ for _ in ()).throw(RuntimeError("no unit")),
    )
    assert terminal._history_scroll_delta(_vte(), controller, 1.0) == 1 * CELL_HEIGHT


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
    """The two flags the fix turns on.  Fallback scrolling is VTE's unit-blind
    history scroll; turning it off makes VTE decline plain scroll events so they
    bubble out to our unit-aware controller.  It does *not* stop VTE handling
    the wheel in the alternate screen or under mouse tracking -- both branches
    sit above the fallback check -- which is why the controller must not be on
    the VTE widget (see test_history_scrolling_stays_behind_vte).
    """
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    calls = {}
    backend.vte = types.SimpleNamespace(
        set_enable_fallback_scrolling=lambda v: calls.__setitem__("fallback", v),
        set_scroll_unit_is_pixels=lambda v: calls.__setitem__("pixels", v),
    )
    backend.configure()
    assert calls == {"fallback": False, "pixels": True}


def test_history_scrolling_stays_behind_vte(monkeypatch):
    """The ordering the fix rests on, pinned where a unit test can hold it.

    ``add_controller`` prepends and ``run_controllers`` stops at the first
    non-gesture TRUE, so a scroll controller on the VTE widget runs *ahead* of
    vte-scroll-controller and swallows the events VTE needs for ESC[A/ESC[B in
    the alternate screen and for button 4/5 mouse reports.  Attaching to the
    parent container is what keeps VTE first; zoom stays on the widget in
    CAPTURE so Ctrl+wheel still works inside a full-screen app.
    """

    class FakeController:
        def __init__(self):
            self.phase = None

        def set_flags(self, _flags):
            pass

        def set_propagation_phase(self, phase):
            self.phase = phase

        def connect(self, _signal, _handler):
            pass

    monkeypatch.setattr(
        terminal_mod.Gtk, 'EventControllerScroll', FakeController, raising=False
    )
    monkeypatch.setattr(terminal_mod, 'is_macos', lambda: False, raising=False)

    terminal = _terminal()
    added = []

    def _host(name):
        return types.SimpleNamespace(
            add_controller=lambda c: added.append((name, c)),
        )

    terminal.terminal_widget = _host('widget')
    terminal.terminal_container = _host('container')
    terminal._scroll_controller = None
    terminal._zoom_controller = None

    terminal._setup_scroll_controllers()

    hosts = dict(added)
    assert set(hosts) == {'widget', 'container'}
    assert hosts['widget'] is terminal._zoom_controller
    assert hosts['container'] is terminal._scroll_controller
    # Capture runs root-to-target, ahead of VTE's own bubble-phase controller.
    assert terminal._zoom_controller.phase is terminal_mod.Gtk.PropagationPhase.CAPTURE
    # The history controller must stay in the default bubble phase.
    assert terminal._scroll_controller.phase is None


def test_pass_through_mode_keeps_the_wheel_working():
    """Pass-through stops SSH Pilot stealing *shortcuts*; it must not stop the
    wheel moving the scrollback.  With VTE's fallback scrolling disabled, our
    controller is the only thing left doing that, so tearing it down alongside
    the shortcut controllers left pass-through with no scrolling at all.
    """
    terminal = _terminal()
    removed = []

    def _host(name):
        return types.SimpleNamespace(
            remove_controller=lambda c: removed.append((name, c)),
        )

    terminal.terminal_widget = _host('widget')
    terminal.terminal_container = _host('container')
    terminal._shortcut_controller = 'shortcut'
    terminal._latin_fallback_controller = None
    terminal._zoom_controller = 'zoom'
    terminal._scroll_controller = 'scroll'
    terminal._search = None

    terminal._remove_custom_shortcut_controllers()

    assert removed == [('widget', 'shortcut'), ('widget', 'zoom')]
    assert terminal._scroll_controller == 'scroll'

    # Real teardown does take it down, and off the container it was added to.
    terminal._remove_scroll_controller()
    assert removed[-1] == ('container', 'scroll')
    assert terminal._scroll_controller is None


def test_pass_through_on_at_startup_still_installs_the_wheel(monkeypatch):
    """The other half of pass-through: not "does the toggle tear it down?" but
    "does a terminal born with pass-through already on ever get it?".

    _install_shortcuts() early-returns while pass-through is on, so hanging the
    scroll controller off the tail of it meant a terminal created with
    terminal.pass_through_mode set in config never installed one -- and with
    VTE's fallback scrolling off, nothing scrolled the scrollback until the user
    toggled pass-through off and back on.  Zoom is a shortcut and stays absent;
    the wheel is not.
    """

    class FakeController:
        def __init__(self):
            self.phase = None

        def set_flags(self, _flags):
            pass

        def set_propagation_phase(self, phase):
            self.phase = phase

        def connect(self, _signal, _handler):
            pass

    monkeypatch.setattr(
        terminal_mod.Gtk, 'EventControllerScroll', FakeController, raising=False
    )
    monkeypatch.setattr(terminal_mod, 'is_macos', lambda: False, raising=False)

    terminal = _terminal()
    added = []

    def _host(name):
        return types.SimpleNamespace(
            add_controller=lambda c: added.append((name, c)),
        )

    terminal.terminal_widget = _host('widget')
    terminal.terminal_container = _host('container')
    terminal._scroll_controller = None
    terminal._zoom_controller = None
    terminal._pass_through_mode = True

    terminal._setup_scroll_controllers()

    assert terminal._scroll_controller is not None
    assert dict(added).get('container') is terminal._scroll_controller
    assert terminal._zoom_controller is None
    assert 'widget' not in dict(added)
