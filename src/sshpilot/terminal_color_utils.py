"""Pure color math for the terminal theme code.

These are backend- and widget-independent helpers that only take and return
``Gdk.RGBA`` (or plain numbers), factored out of ``TerminalWidget`` so the math
lives in one small, testable place with no Gtk dependency.

Note: ``terminal_backends.py`` still carries its own ``_clone_rgba`` /
``_relative_luminance`` (identical to these) and a *different*
``_get_contrast_color`` (returns ``#000000`` with no alpha, vs. ``#1B1B1D`` +
opaque here). They are intentionally left as-is — merging the divergent contrast
helper would change backend behavior.
"""

from gi.repository import Gdk


def mix_rgba(base: Gdk.RGBA, other: Gdk.RGBA, ratio: float) -> Gdk.RGBA:
    """Linearly blend ``base`` toward ``other`` by ``ratio`` (clamped 0..1)."""
    ratio = max(0.0, min(1.0, ratio))
    mixed = Gdk.RGBA()
    mixed.red = base.red * (1.0 - ratio) + other.red * ratio
    mixed.green = base.green * (1.0 - ratio) + other.green * ratio
    mixed.blue = base.blue * (1.0 - ratio) + other.blue * ratio
    mixed.alpha = base.alpha * (1.0 - ratio) + other.alpha * ratio
    return mixed


def clone_rgba(rgba: Gdk.RGBA) -> Gdk.RGBA:
    """Return an independent copy of ``rgba``."""
    clone = Gdk.RGBA()
    clone.red = rgba.red
    clone.green = rgba.green
    clone.blue = rgba.blue
    clone.alpha = rgba.alpha
    return clone


def relative_luminance(rgba: Gdk.RGBA) -> float:
    """WCAG relative luminance of ``rgba`` (sRGB, 0..1)."""
    def to_linear(channel: float) -> float:
        if channel <= 0.03928:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    r_lin = to_linear(rgba.red)
    g_lin = to_linear(rgba.green)
    b_lin = to_linear(rgba.blue)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def get_contrast_color(background: Gdk.RGBA) -> Gdk.RGBA:
    """Opaque near-black (``#1B1B1D``) on light backgrounds, white on dark."""
    luminance = relative_luminance(background)
    contrast = Gdk.RGBA()
    if luminance > 0.5:
        contrast.parse('#1B1B1D')
    else:
        contrast.parse('#FFFFFF')
    contrast.alpha = 1.0
    return contrast
