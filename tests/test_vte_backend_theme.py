"""VTE theme rendering remains an implementation detail of its backend."""

import types

import pytest

pytest.importorskip("gi")

from sshpilot.terminal_backends import VTETerminalBackend


def test_vte_backend_applies_palette_cursor_selection_and_font(monkeypatch):
    class Color:
        def parse(self, value):
            self.value = value
            self.red = self.green = self.blue = 0.0
            self.alpha = 1.0
            return True

    from sshpilot import terminal_backends
    monkeypatch.setattr(terminal_backends.Gdk, "RGBA", Color)
    monkeypatch.setattr(
        terminal_backends.Pango.FontDescription,
        "from_string",
        lambda value: types.SimpleNamespace(value=value, get_size=lambda: 13),
    )
    calls = {}
    vte = types.SimpleNamespace(
        set_colors=lambda fg, bg, palette: calls.update(colors=(fg, bg, palette)),
        set_color_cursor=lambda color: calls.update(cursor=color),
        set_color_highlight=lambda color: calls.update(selection_bg=color),
        set_color_highlight_foreground=lambda color: calls.update(selection_fg=color),
        set_font=lambda font: calls.update(font=font),
        queue_draw=lambda: calls.update(redrawn=True),
        add_css_class=lambda css_class: None,
    )
    profile = {
        "foreground": "#eeeeec",
        "background": "#2e3436",
        "cursor_color": "#ffffff",
        "highlight_background": "#4a90e2",
        "highlight_foreground": "#ffffff",
        "font": "Monospace 13",
        "palette": ["#000000"] * 16,
    }
    owner = types.SimpleNamespace(
        config=types.SimpleNamespace(
            get_terminal_profile=lambda _name: profile,
            get_setting=lambda _key, default=None: default,
        ),
        scrolled_window=types.SimpleNamespace(add_css_class=lambda _name: None),
        add_css_class=lambda _name: None,
        _get_group_color_rgba=lambda: None,
    )
    backend = object.__new__(VTETerminalBackend)
    backend.owner = owner
    backend.vte = vte

    backend.apply_theme("dark")

    assert len(calls["colors"][2]) == 16
    assert calls["cursor"].value == "#ffffff"
    assert calls["selection_bg"].value == "#4a90e2"
    assert calls["selection_fg"].value == "#ffffff"
    assert calls["font"].get_size() > 0
    assert calls["redrawn"] is True
