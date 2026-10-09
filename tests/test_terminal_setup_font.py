"""New terminals start in the configured font, whatever the backend.

VTE re-read ``terminal.font`` in its ``apply_theme()``, but the xterm.js
backend's theme never touches the font, so the "Monospace 12" that
``setup_terminal()`` used to hardcode stuck: after switching to xterm.js every
new tab ignored the font the user had picked.
"""

import types

from sshpilot import terminal as terminal_mod


class RecordingBackend:
    def __init__(self):
        self.fonts = []

    def set_font(self, font_desc):
        self.fonts.append(font_desc)

    def configure(self, _options):
        pass

    def apply_theme(self, _theme_name=None):
        pass

    def setup_link_handling(self, *_handlers):
        pass


class FakeFontDescription:
    """Stands in for Pango, which the test suite stubs out."""

    def __init__(self):
        self.family = None
        self.size = None

    @staticmethod
    def from_string(text):
        return text

    def set_family(self, family):
        self.family = family

    def set_size(self, size):
        self.size = size


def _setup(monkeypatch, font_setting):
    monkeypatch.setattr(
        terminal_mod, 'Pango',
        types.SimpleNamespace(FontDescription=FakeFontDescription, SCALE=1024),
    )
    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)
    settings = {} if font_setting is None else {'terminal.font': font_setting}
    terminal.config = types.SimpleNamespace(
        get_setting=lambda key, default=None: settings.get(key, default)
    )
    terminal.backend = RecordingBackend()
    terminal._pass_through_mode = False
    terminal._apply_pass_through_mode = lambda _enabled: None
    terminal._setup_scroll_controllers = lambda: None
    terminal._setup_context_menu = lambda: None
    terminal.setup_terminal()
    return terminal.backend.fonts


def test_setup_terminal_applies_the_configured_font(monkeypatch):
    assert _setup(monkeypatch, 'JetBrains Mono 24') == ['JetBrains Mono 24']


def test_setup_terminal_falls_back_without_a_font_setting(monkeypatch):
    assert _setup(monkeypatch, None) == ['Monospace 12']
    assert _setup(monkeypatch, '') == ['Monospace 12']
