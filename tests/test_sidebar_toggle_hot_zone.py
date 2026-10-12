"""Header-bar hot zone and popup inset for the hidden sidebar (window.py)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.window import MainWindow

LTR = 'ltr'


def _rect(x, y, w, h):
    return SimpleNamespace(get_x=lambda: x, get_y=lambda: y,
                           get_width=lambda: w, get_height=lambda: h)


def _widget(rect, mapped=True):
    w = MagicMock()
    w.get_mapped.return_value = mapped
    w.compute_bounds.return_value = (True, rect)
    return w


class _Window:
    _in_sidebar_toggle_zone = MainWindow._in_sidebar_toggle_zone
    _sidebar_popup_top_inset = MainWindow._sidebar_popup_top_inset

    def __init__(self, header_mapped=True):
        # Toggle button at x 12..46 inside a 46px header bar at the top.
        self.sidebar_toggle_button = _widget(_rect(12, 6, 34, 34))
        self.header_bar = _widget(_rect(0, 0, 1000, 46), header_mapped)
        self._content_overlay = MagicMock()
        self._content_overlay.get_direction.return_value = LTR


def test_zone_runs_from_the_edge_through_the_toggle():
    w = _Window()
    assert w._in_sidebar_toggle_zone(0, 20)       # window controls / padding
    assert w._in_sidebar_toggle_zone(30, 2)       # the button, top of the header
    assert w._in_sidebar_toggle_zone(46, 45)
    assert not w._in_sidebar_toggle_zone(60, 20)  # the next header button
    assert not w._in_sidebar_toggle_zone(30, 60)  # below the header


def test_no_zone_while_the_header_is_unmapped():
    w = _Window(header_mapped=False)
    assert not w._in_sidebar_toggle_zone(30, 20)
    assert w._sidebar_popup_top_inset() == 0


def test_popup_starts_below_the_header():
    assert _Window()._sidebar_popup_top_inset() == 46
