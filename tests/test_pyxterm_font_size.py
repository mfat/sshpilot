"""PyXterm font size: Pango/CSS points → xterm.js CSS pixels."""

from sshpilot.terminal_backends import pango_points_to_xterm_px


def test_points_to_css_pixels_standard_sizes():
    # CSS: 1pt = 96/72 px → 12pt = 16px (matches Preferences preview using Npt).
    assert pango_points_to_xterm_px(12) == 16
    assert pango_points_to_xterm_px(9) == 12
    assert pango_points_to_xterm_px(18) == 24


def test_points_to_css_pixels_with_zoom_scale():
    assert pango_points_to_xterm_px(12, scale=1.5) == 24
    assert pango_points_to_xterm_px(12, scale=0.5) == 8


def test_points_to_css_pixels_minimum_is_one():
    assert pango_points_to_xterm_px(0) == 1
    assert pango_points_to_xterm_px(0.1, scale=0.1) == 1
