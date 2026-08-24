"""The header-bar Settings button must advertise the *resolved* shortcut.

Its tooltip used to be a bare "Settings" even though the action ships a default
accelerator and the shortcut editor can rebind it, so the one place a user
looks for the key never mentioned it. These tests pin the same split the
sidebar toggle uses: the accessible name stays the bare action, while the
tooltip carries whatever accelerator is currently in effect — and drops the
parenthetical entirely when the shortcut is disabled.
"""

from types import SimpleNamespace

import pytest

try:
    import sshpilot.window as window_module
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    window_module = None
    MainWindow = None

pytestmark = pytest.mark.skipif(
    MainWindow is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


class _RecordingButton:
    """Stands in for the icon button; records what the builder puts on it."""

    def __init__(self):
        self.tooltip = None
        self.css_classes = []
        self.connected = []

    def add_css_class(self, name):
        self.css_classes.append(name)

    def set_can_focus(self, value):
        self.can_focus = value

    def set_tooltip_text(self, text):
        self.tooltip = text

    def connect(self, signal, handler):
        self.connected.append(signal)


@pytest.fixture
def button_factory(monkeypatch):
    from sshpilot import icon_utils

    def build(shortcuts):
        button = _RecordingButton()
        monkeypatch.setattr(
            icon_utils, 'new_button_from_icon_name', lambda *_a, **_k: button
        )
        window = SimpleNamespace(
            _get_safe_current_shortcuts=lambda: shortcuts,
            show_preferences=lambda *_a: None,
        )
        assert MainWindow._build_preferences_button(window) is button
        return button

    return build


def test_tooltip_names_the_default_shortcut(button_factory):
    button = button_factory({'preferences': ['<primary>comma']})

    label = window_module._accelerator_label('<primary>comma')
    assert button.tooltip == f'Settings ({label})'
    assert 'clicked' in button.connected


def test_tooltip_follows_a_rebound_shortcut(button_factory):
    button = button_factory({'preferences': ['<Alt>p']})

    assert button.tooltip == f"Settings ({window_module._accelerator_label('<Alt>p')})"


def test_every_bound_accelerator_is_listed(button_factory):
    button = button_factory({'preferences': ['<primary>comma', '<Alt>p']})

    labels = ', '.join(
        window_module._accelerator_label(a) for a in ('<primary>comma', '<Alt>p')
    )
    assert button.tooltip == f'Settings ({labels})'


def test_tooltip_drops_the_parenthetical_when_the_shortcut_is_disabled(button_factory):
    assert button_factory({'preferences': []}).tooltip == 'Settings'
    assert button_factory({}).tooltip == 'Settings'
