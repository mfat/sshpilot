"""Animated sketch of the hover reveal for Settings › Sidebar › Behavior.

The pointer rests on the header bar's sidebar button, the sidebar floats out
over the terminal (see sidebar_hover_reveal.py), the pointer browses it and
leaves, and the sidebar slides away again. Drawn with Cairo so it follows the
light/dark style and accent colour; with animations off it shows one still
frame of the sidebar revealed.
"""

import math
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk

WIDTH, HEIGHT = 168, 105
LOOP_S = 4.4

_HEADER_H = 14
_BUTTON = (6, 3, 12, 8)  # x, y, w, h of the sidebar button in the header
_PANEL_W = 58
_START = (118.0, 70.0)
_ON_BUTTON = (12.0, 7.0)
_ON_ROW = (32.0, 45.0)
_AWAY = (128.0, 52.0)


def _ease(t):
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def _span(now, start, end):
    return _ease((now - start) / (end - start))


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def frame_at(now):
    """(pointer xy, pointer opacity, panel 0..1, button hovered, hovered row)."""
    t = now % LOOP_S
    if t < 0.9:
        pointer = _lerp(_START, _ON_BUTTON, _span(t, 0.1, 0.9))
    elif t < 1.7:
        pointer = _ON_BUTTON
    elif t < 2.6:
        pointer = _lerp(_ON_BUTTON, _ON_ROW, _span(t, 1.7, 2.3))
    else:
        pointer = _lerp(_ON_ROW, _AWAY, _span(t, 2.6, 3.2))
    if t < 1.25:
        panel = 0.0
    elif t < 3.3:
        panel = _span(t, 1.25, 1.55)
    else:
        panel = 1 - _span(t, 3.3, 3.6)
    opacity = 1.0 - _span(t, 3.8, 4.2) if t >= 3.8 else min(1.0, t / 0.1)
    hovered_row = 2 if 2.1 <= t < 2.7 else None
    return pointer, opacity, panel, 0.6 <= t < 1.7, hovered_row


def _rgba(cr, color, alpha=1.0):
    cr.set_source_rgba(color[0], color[1], color[2], color[3] * alpha)


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class SidebarPeekPreview(Gtk.DrawingArea):
    """A looping miniature of the sidebar hover reveal."""

    def __init__(self):
        super().__init__()
        self.set_content_width(WIDTH)
        self.set_content_height(HEIGHT)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("The pointer rests on the sidebar button and the sidebar "
               "slides out over the terminal")],
        )
        self.set_draw_func(self._draw)
        self._tick_id = 0
        self.connect('map', self._on_map)
        self.connect('unmap', self._on_unmap)

    @staticmethod
    def _animations_enabled():
        settings = Gtk.Settings.get_default()
        return settings is None or settings.get_property('gtk-enable-animations')

    def _on_map(self, *_args):
        if self._tick_id == 0 and self._animations_enabled():
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_unmap(self, *_args):
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _on_tick(self, _widget, _clock):
        self.queue_draw()
        return True

    def _now(self):
        clock = self.get_frame_clock()
        if not self._tick_id or clock is None:
            return 2.2  # still frame: sidebar out, pointer on a row
        return clock.get_frame_time() / 1_000_000

    def _draw(self, _area, cr, width, height):
        dark = Adw.StyleManager.get_default().get_dark()
        fg = self.get_color()
        fg = (fg.red, fg.green, fg.blue, 1.0)
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        accent = (accent.red, accent.green, accent.blue, 1.0)
        window_bg = (0.13, 0.13, 0.15, 1) if dark else (0.98, 0.98, 0.98, 1)
        panel_bg = (0.18, 0.18, 0.2, 1) if dark else (1, 1, 1, 1)
        pointer_xy, pointer_alpha, panel, on_button, hovered_row = frame_at(self._now())

        # Window frame.
        _rounded(cr, 0.5, 0.5, width - 1, height - 1, 6)
        _rgba(cr, window_bg)
        cr.fill_preserve()
        _rgba(cr, fg, 0.12)
        cr.set_line_width(1)
        cr.stroke()

        # Header bar and its sidebar button.
        bx, by, bw, bh = _BUTTON
        _rounded(cr, bx, by, bw, bh, 2)
        _rgba(cr, accent if on_button else fg, 0.25 if on_button else 0.08)
        cr.fill()
        cr.rectangle(bx + 2.5, by + 2, 2, bh - 4)
        _rgba(cr, fg, 0.6)
        cr.fill()
        _rounded(cr, width / 2 - 20, 4, 40, 6, 2)
        _rgba(cr, fg, 0.08)
        cr.fill()

        # Terminal.
        tx, ty, tw, th = 3, _HEADER_H, width - 6, height - _HEADER_H - 3
        _rounded(cr, tx, ty, tw, th, 3)
        cr.set_source_rgb(0, 0, 0)
        cr.fill()
        for i, (w, colour) in enumerate(((54, (0.31, 0.6, 0.02, 1)), (88, (0.83, 0.84, 0.81, 1)),
                                         (70, (0.83, 0.84, 0.81, 1)), (30, (0.31, 0.6, 0.02, 1)))):
            cr.rectangle(tx + 6, ty + 7 + i * 8, w, 3)
            _rgba(cr, colour, 0.75)
            cr.fill()

        # Floating sidebar.
        if panel > 0:
            px = tx + 3 - (_PANEL_W + 6) * (1 - panel)
            py, ph = ty + 3, th - 6
            cr.save()
            cr.push_group()
            _rounded(cr, px + 1, py + 2, _PANEL_W, ph, 4)
            cr.set_source_rgba(0, 0, 0, 0.35)
            cr.fill()
            _rounded(cr, px, py, _PANEL_W, ph, 4)
            _rgba(cr, panel_bg)
            cr.fill_preserve()
            _rgba(cr, fg, 0.12)
            cr.stroke()
            dots = ((0.93, 0.2, 0.23, 1), (1.0, 0.47, 0.0, 1), (0.57, 0.25, 0.67, 1),
                    (0.96, 0.83, 0.18, 1))
            for i, dot in enumerate(dots):
                ry = py + 6 + i * 11
                if hovered_row == i:
                    _rounded(cr, px + 3, ry - 3, _PANEL_W - 6, 10, 2)
                    _rgba(cr, accent, 0.25)
                    cr.fill()
                cr.arc(px + 9, ry + 2, 2, 0, 2 * math.pi)
                _rgba(cr, dot)
                cr.fill()
                cr.rectangle(px + 14, ry + 0.5, 22 + (i % 2) * 8, 3)
                _rgba(cr, fg, 0.4)
                cr.fill()
            cr.pop_group_to_source()
            cr.paint_with_alpha(panel)
            cr.restore()

        # Pointer arrow.
        if pointer_alpha > 0:
            x, y = pointer_xy
            cr.move_to(x, y)
            cr.rel_line_to(0, 11)
            cr.rel_line_to(3, -3)
            cr.rel_line_to(2.5, 5)
            cr.rel_line_to(2, -1)
            cr.rel_line_to(-2.5, -5)
            cr.rel_line_to(4, 0)
            cr.close_path()
            cr.set_source_rgba(1, 1, 1, pointer_alpha)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, pointer_alpha)
            cr.set_line_width(1)
            cr.stroke()
