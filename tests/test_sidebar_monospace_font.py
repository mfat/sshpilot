"""Interface monospace-font preference: family resolution and CSS toggle."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.sidebar import (
    _css_escape_font_family,
    _interface_monospace_family,
    apply_interface_monospace_font,
)
from sshpilot.config import Config
from sshpilot.core.settings.migration import ensure_config_defaults


def test_default_config_has_monospace_off():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults["ui"]["monospace_font"] is False
    assert "sidebar_monospace_font" not in defaults["ui"]


def test_migrate_sidebar_monospace_to_interface_key():
    config = {"ui": {"sidebar_monospace_font": True}}
    config, updated = ensure_config_defaults(config)
    assert updated is True
    assert config["ui"]["monospace_font"] is True
    assert "sidebar_monospace_font" not in config["ui"]


def test_css_escape_font_family_quotes_and_backslashes():
    assert _css_escape_font_family('JetBrains Mono') == "JetBrains Mono"
    assert _css_escape_font_family('Foo "Bar"') == 'Foo \\"Bar\\"'
    assert _css_escape_font_family("a\\b") == "a\\\\b"


def test_interface_monospace_family_uses_terminal_font():
    config = MagicMock()
    config.get_setting.side_effect = lambda key, default=None: {
        "terminal.font": "JetBrains Mono 13",
    }.get(key, default)
    assert _interface_monospace_family(config) == "JetBrains Mono"


def test_font_family_from_string_strips_style_and_size():
    from sshpilot.sidebar import _font_family_from_string

    assert _font_family_from_string("Source Code Pro Bold 12") == "Source Code Pro"
    assert _font_family_from_string("Monospace 12") == "Monospace"
    assert _font_family_from_string("") == "Monospace"


def test_interface_monospace_family_falls_back_to_monospace():
    assert _interface_monospace_family(None) == "Monospace"
    config = MagicMock()
    config.get_setting.side_effect = lambda key, default=None: default
    assert _interface_monospace_family(config) == "Monospace"


def test_apply_interface_monospace_font_skips_when_disabled(monkeypatch):
    display = SimpleNamespace()
    monkeypatch.setattr(
        "sshpilot.sidebar.Gdk.Display.get_default", lambda: display
    )
    provider_calls = []

    class FakeProvider:
        def load_from_data(self, data):
            provider_calls.append(data)

    monkeypatch.setattr("sshpilot.sidebar.Gtk.CssProvider", FakeProvider)
    monkeypatch.setattr(
        "sshpilot.sidebar.Gtk.StyleContext.add_provider_for_display",
        lambda *a, **k: provider_calls.append("added"),
    )

    config = MagicMock()
    config.get_setting.side_effect = lambda key, default=None: {
        "ui.monospace_font": False,
        "terminal.font": "JetBrains Mono 12",
    }.get(key, default)

    apply_interface_monospace_font(config)
    assert provider_calls == []
    assert not hasattr(display, "_interface_monospace_css_provider")


def test_apply_interface_monospace_font_uses_terminal_family(monkeypatch):
    display = SimpleNamespace()
    monkeypatch.setattr(
        "sshpilot.sidebar.Gdk.Display.get_default", lambda: display
    )
    loaded = []

    class FakeProvider:
        def load_from_data(self, data):
            loaded.append(data.decode("utf-8"))

    added = []

    def add_provider(disp, provider, priority):
        added.append((disp, provider, priority))
        disp._interface_monospace_css_provider = provider

    monkeypatch.setattr("sshpilot.sidebar.Gtk.CssProvider", FakeProvider)
    monkeypatch.setattr(
        "sshpilot.sidebar.Gtk.StyleContext.add_provider_for_display",
        add_provider,
    )
    monkeypatch.setattr(
        "sshpilot.sidebar.Gtk.StyleContext.remove_provider_for_display",
        lambda *a, **k: None,
    )

    config = MagicMock()
    config.get_setting.side_effect = lambda key, default=None: {
        "ui.monospace_font": True,
        "terminal.font": "JetBrains Mono 13",
    }.get(key, default)

    apply_interface_monospace_font(config)

    assert len(added) == 1
    assert hasattr(display, "_interface_monospace_css_provider")
    css = loaded[0]
    assert 'font-family: "JetBrains Mono", monospace;' in css
    assert "*" in css
    assert ".connection-sidebar" not in css
