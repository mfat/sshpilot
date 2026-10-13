"""Hover reveal of the hidden sidebar (sidebar_hover_reveal.py).

Drives the motion handlers directly with a stub popup and a fake GLib timer
that the tests fire by hand, so they run under the suite's stubbed ``gi`` and
never wait on a main loop.
"""
import sys
from unittest.mock import MagicMock

import pytest

from sshpilot import sidebar_hover_reveal as mod
from sshpilot.sidebar_hover_reveal import SidebarHoverReveal

Gtk = mod.Gtk
BUTTON1 = 1


class _Timers:
    def __init__(self):
        self.pending = {}
        self._next = 0

    def timeout_add(self, _ms, callback):
        self._next += 1
        self.pending[self._next] = callback
        return self._next

    def source_remove(self, source_id):
        del self.pending[source_id]


@pytest.fixture(autouse=True)
def _fake_glib(monkeypatch):
    timers = _Timers()
    monkeypatch.setattr(mod, 'GLib', MagicMock(
        timeout_add=timers.timeout_add,
        source_remove=timers.source_remove,
        SOURCE_REMOVE=False,
    ))
    monkeypatch.setattr(mod, '_BUTTONS_MASK', BUTTON1)


class _Popup:
    def __init__(self):
        self.visible = False
        self.panel = None

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


def _controller(state=0):
    c = MagicMock()
    c.get_current_event_state.return_value = state
    return c


def _toggle_zone(x, y):
    return x <= 50 and y <= 46


def _reveal(hidden=True, keep_open=False, width=800, hot_zone=None):
    overlay = MagicMock()
    overlay.get_direction.return_value = Gtk.TextDirection.LTR
    overlay.get_width.return_value = width
    overlay.get_root.return_value = None
    popup = _Popup()
    r = SidebarHoverReveal.__new__(SidebarHoverReveal)
    r._overlay = overlay
    r._popup_func = lambda: popup
    r._is_hidden = lambda: hidden
    r._keep_open = lambda: keep_open
    r._in_hot_zone = hot_zone or _toggle_zone
    r._revealed = False
    r._in_zone = False
    r._armed = False
    r._reveal_id = 0
    r._hide_id = 0
    return r, popup


def _fire(r, attr, callback):
    """Run a pending timer now instead of waiting for it."""
    assert getattr(r, attr), f"no {attr} timer pending"
    mod.GLib.source_remove(getattr(r, attr))
    callback()


def test_dragging_onto_the_hot_zone_does_not_reveal():
    r, popup = _reveal()
    r._on_motion(_controller(BUTTON1), 30, 20)
    assert not r._reveal_id


def test_resting_on_the_hot_zone_reveals_the_hidden_sidebar():
    r, popup = _reveal(hot_zone=lambda x, y: 20 <= x <= 60 and y <= 46)
    r._on_motion(_controller(), 40, 20)
    _fire(r, '_reveal_id', r._reveal)
    assert popup.visible


def test_hiding_with_the_pointer_on_the_toggle_does_not_pop_it_back_out():
    hidden = [False]
    r, popup = _reveal()
    r._is_hidden = lambda: hidden[0]
    r._on_motion(_controller(), 30, 20)    # on the toggle, sidebar docked
    hidden[0] = True                       # the click hides it
    r._on_motion(_controller(), 31, 20)    # the hand rests, jitters
    assert not r._reveal_id
    r._on_motion(_controller(), 30, 300)   # leave the zone ...
    r._on_motion(_controller(), 30, 20)    # ... and come back: armed
    assert r._reveal_id
    r._cancel_reveal()


def test_the_window_edge_below_the_header_does_not_reveal():
    r, popup = _reveal()
    r._on_motion(_controller(), 0, 400)
    assert not r._reveal_id


def test_hot_zone_does_nothing_while_the_sidebar_is_docked():
    r, popup = _reveal(hidden=False, hot_zone=lambda x, y: True)
    r._on_motion(_controller(), 40, 20)
    assert not r._reveal_id


def test_leaving_the_hot_zone_before_the_delay_cancels_the_reveal():
    r, popup = _reveal(hot_zone=lambda x, y: y <= 46)
    r._on_motion(_controller(), 40, 20)
    r._on_motion(_controller(), 40, 300)
    assert not r._reveal_id


def _revealed_with_panel(**kw):
    r, popup = _reveal(**kw)
    r._on_motion(_controller(), 30, 20)
    _fire(r, '_reveal_id', r._reveal)
    bounds = MagicMock()
    bounds.get_x.return_value = 0
    bounds.get_width.return_value = 280
    popup.panel = MagicMock()
    popup.panel.compute_bounds.return_value = (True, bounds)
    return r, popup


def test_moving_off_the_panel_hides_it():
    r, popup = _revealed_with_panel()
    r._on_motion(_controller(), 280 + mod.HIDE_MARGIN_PX + 1, 200)
    _fire(r, '_hide_id', r._hide)
    assert not popup.visible and not r.revealed


def test_coming_back_onto_the_panel_cancels_the_hide():
    r, popup = _revealed_with_panel()
    r._on_motion(_controller(), 500, 200)
    r._on_motion(_controller(), 100, 200)
    assert not r._hide_id and popup.visible


def test_search_opened_from_the_reveal_keeps_it_open():
    r, popup = _revealed_with_panel(keep_open=True)
    r._on_motion(_controller(), 500, 200)
    _fire(r, '_hide_id', r._hide)
    assert popup.visible


def test_a_popup_opened_by_search_is_not_hover_hidden():
    r, popup = _reveal()
    popup.show()          # Ctrl+F detached it, not the edge
    r._on_motion(_controller(), 500, 200)
    assert not r._hide_id and popup.visible


@pytest.mark.skipif(
    not hasattr(sys.modules.get('gi'), 'version_info'),
    reason="needs real GTK (gi is stubbed under the suite)",
)
def test_real_overlay_takes_a_capture_motion_controller():
    overlay = Gtk.Overlay()
    r = SidebarHoverReveal(overlay, lambda: None, lambda: True, lambda: False,
                           _toggle_zone)
    assert r._motion.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    assert r._motion.get_widget() is overlay
