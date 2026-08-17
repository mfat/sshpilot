"""VTE theme rendering remains an implementation detail of its backend."""

import types

import pytest

pytest.importorskip("gi")

from sshpilot.terminal_backends import VTETerminalBackend


def test_vte_backend_applies_palette_cursor_selection_and_font(monkeypatch):
    class Color:
        def parse(self, value):
            self.value = value
            named = {"black": "#000000", "white": "#ffffff"}
            value = named.get(value.lower(), value)
            self.red = int(value[1:3], 16) / 255
            self.green = int(value[3:5], 16) / 255
            self.blue = int(value[5:7], 16) / 255
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
    styled = {"owner": [], "container": [], "scrolled": [], "vte": []}
    vte.add_css_class = lambda name: styled["vte"].append(name)
    owner = types.SimpleNamespace(
        config=types.SimpleNamespace(
            get_terminal_profile=lambda _name: profile,
            get_setting=lambda _key, default=None: default,
        ),
        container_box=types.SimpleNamespace(
            add_css_class=lambda name: styled["container"].append(name)
        ),
        terminal_container=types.SimpleNamespace(
            add_css_class=lambda name: styled["scrolled"].append(name)
        ),
        add_css_class=lambda name: styled["owner"].append(name),
        _get_group_color_rgba=lambda: None,
    )
    backend = object.__new__(VTETerminalBackend)
    backend.owner = owner
    backend.vte = vte
    backend._css_class = "terminal-bg-test"
    backend._background_provider = None

    backend.apply_theme("dark")

    assert len(calls["colors"][2]) == 16
    assert calls["cursor"].value == "#ffffff"
    assert calls["selection_bg"].value == "#4a90e2"
    assert calls["selection_fg"].value == "#ffffff"
    assert calls["font"].get_size() > 0
    assert calls["redrawn"] is True
    assert styled["owner"] == []
    assert styled["container"] == []
    assert styled["scrolled"] == ["terminal-bg-test"]
    assert styled["vte"] == ["terminal-bg-test"]


def test_group_theme_keeps_selection_distinct(monkeypatch):
    calls = {}

    class Color:
        def parse(self, value):
            value = {"black": "#000000", "white": "#ffffff"}.get(value.lower(), value)
            self.red = int(value[1:3], 16) / 255
            self.green = int(value[3:5], 16) / 255
            self.blue = int(value[5:7], 16) / 255
            self.alpha = 1.0
            return True

    from sshpilot import terminal_backends
    monkeypatch.setattr(terminal_backends.Gdk, "RGBA", Color)
    monkeypatch.setattr(terminal_backends.Pango.FontDescription, "from_string", lambda _v: object())
    group = Color()
    group.parse("#203040")
    owner = types.SimpleNamespace(
        config=types.SimpleNamespace(
            get_terminal_profile=lambda _name: {
                "foreground": "#ffffff", "background": "#000000",
                "font": "Monospace 12", "palette": [],
            },
            get_setting=lambda key, default=None: (
                True if key == "ui.use_group_color_in_terminal" else default
            ),
        ),
        _get_group_color_rgba=lambda: group,
        terminal_container=types.SimpleNamespace(add_css_class=lambda _name: None),
        add_css_class=lambda _name: None,
    )
    backend = object.__new__(VTETerminalBackend)
    backend.owner = owner
    backend._css_class = "terminal-bg-test"
    backend._background_provider = None
    backend.vte = types.SimpleNamespace(
        set_colors=lambda fg, bg, palette: calls.update(fg=fg, bg=bg),
        set_color_cursor=lambda color: None,
        set_color_highlight=lambda color: calls.update(selection_bg=color),
        set_color_highlight_foreground=lambda color: calls.update(selection_fg=color),
        set_font=lambda font: None, queue_draw=lambda: None,
        add_css_class=lambda name: None,
    )

    backend.apply_theme("group")

    normal = (calls["bg"].red, calls["bg"].green, calls["bg"].blue)
    selected = (
        calls["selection_bg"].red,
        calls["selection_bg"].green,
        calls["selection_bg"].blue,
    )
    assert selected != normal


def test_optional_vte_configuration_and_links_are_non_fatal(monkeypatch):
    from sshpilot import terminal_backends
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace()
    monkeypatch.setattr(
        terminal_backends.Vte,
        "Regex",
        types.SimpleNamespace(new_for_match=lambda *_args: (_ for _ in ()).throw(RuntimeError())),
    )

    backend.configure({"encoding": "UTF-8"})
    backend.setup_link_handling(lambda *_a: None, lambda *_a: None, lambda *_a: None)

    assert backend.supports_feature("hyperlinks") is False


def test_vte_spawn_uses_modern_binding_and_normalizes_callback_user_data():
    captured = {}

    def spawn_async(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)

    vte = types.SimpleNamespace(spawn_async=spawn_async)
    backend = object.__new__(VTETerminalBackend)
    backend.vte = vte
    completed = []
    callback = lambda *args: completed.append(args)
    user_data = object()

    backend.spawn_async(["/bin/sh"], callback=callback, user_data=user_data)

    assert captured["args"] == ()
    assert captured["kwargs"]["callback"] is not callback
    assert captured["kwargs"]["cancellable"] is None
    assert captured["kwargs"]["user_data"] is user_data
    captured["kwargs"]["callback"](vte, 123, None, "native-user-data")
    assert completed == [(vte, 123, None, user_data)]


def test_vte_spawn_falls_back_to_legacy_child_setup_data_binding():
    calls = []

    class LegacyVte:
        def spawn_async(self, *args, **kwargs):
            if kwargs:
                raise TypeError("legacy binding does not accept keywords")
            calls.append(args)

    backend = object.__new__(VTETerminalBackend)
    backend.vte = LegacyVte()
    completed = []
    callback = lambda *args: completed.append(args)
    user_data = object()

    backend.spawn_async(["/bin/sh"], callback=callback, user_data=user_data)

    assert len(calls) == 1
    assert calls[0][6] is None  # legacy child_setup_data
    assert calls[0][9] is not None  # normalized callback closure
    assert calls[0][10] is user_data  # native callback user_data
    calls[0][9](backend.vte, 456, None, "native-user-data")
    assert completed == [(backend.vte, 456, None, user_data)]
