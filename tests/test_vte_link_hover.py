"""Regression tests for the #1104 follow-up.

The pointer-motion path must never call ``check_hyperlink_at`` (that native
probe dereferences VTE screen state and segfaulted from ``_on_vte_motion``):
OSC 8 hover comes from VTE's ``hyperlink-hover-uri-changed`` signal and the
motion handler probes the plain-text regex only. Repeated link setup (init +
``setup_local_shell``) must not stack duplicate hover handlers/controllers.
"""

import types

import pytest

pytest.importorskip("gi")

from sshpilot import terminal_backends
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import VTETerminalBackend


def _make_backend(**vte_attrs):
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(**vte_attrs)
    return backend


# --------------------------------------------------------------------------
# Backend probes
# --------------------------------------------------------------------------

def test_plain_text_link_at_never_touches_hyperlink_probe():
    def _forbidden(x, y):
        raise AssertionError("check_hyperlink_at called from regex-only probe")

    backend = _make_backend(
        check_hyperlink_at=_forbidden,
        check_match_at=lambda x, y: ("https://example.com", 7),
    )
    assert backend.plain_text_link_at(3, 4) == "https://example.com"


def test_plain_text_link_at_handles_plain_string_result():
    backend = _make_backend(check_match_at=lambda x, y: "https://example.com")
    assert backend.plain_text_link_at(0, 0) == "https://example.com"


def test_hyperlink_at_prefers_osc8_then_falls_back_to_regex():
    osc8 = _make_backend(check_hyperlink_at=lambda x, y: "https://osc8.example")
    assert osc8.hyperlink_at(0, 0) == "https://osc8.example"

    regex_only = _make_backend(
        check_hyperlink_at=lambda x, y: None,
        check_match_at=lambda x, y: ("https://plain.example", 1),
    )
    assert regex_only.hyperlink_at(0, 0) == "https://plain.example"


def test_probes_return_none_after_destroy():
    backend = _make_backend(
        check_hyperlink_at=lambda x, y: "https://osc8.example",
        check_match_at=lambda x, y: ("https://plain.example", 1),
    )
    backend._destroyed = True
    assert backend.hyperlink_at(0, 0) is None
    assert backend.plain_text_link_at(0, 0) is None


# --------------------------------------------------------------------------
# Idempotent link setup
# --------------------------------------------------------------------------

class _FakeController:
    def __init__(self):
        self.connected = []

    def connect(self, name, callback):
        self.connected.append(name)


class _FakeVte:
    def __init__(self):
        self.signals = {}
        self._next_id = 0
        self.disconnected = []
        self.controllers = []
        self.removed_controllers = []

    def match_add_regex(self, regex, flags):
        return 1

    def match_set_cursor_name(self, tag, name):
        pass

    def add_controller(self, controller):
        self.controllers.append(controller)

    def remove_controller(self, controller):
        self.removed_controllers.append(controller)

    def connect(self, name, callback):
        self._next_id += 1
        self.signals[self._next_id] = (name, callback)
        return self._next_id

    def disconnect(self, handler_id):
        self.disconnected.append(handler_id)
        self.signals.pop(handler_id, None)

    def queue_draw(self):
        pass


def test_setup_link_handling_disconnects_previous_hover_handler(monkeypatch):
    monkeypatch.setattr(
        terminal_backends.Vte, "Regex",
        types.SimpleNamespace(new_for_match=lambda *args: object()),
    )
    monkeypatch.setattr(terminal_backends.Gtk, "EventControllerMotion", _FakeController)

    backend = object.__new__(VTETerminalBackend)
    backend.vte = _FakeVte()

    noop = lambda *args: None
    backend.setup_link_handling(noop, noop, noop, noop)
    first_hover = backend._hover_handler
    first_controller = backend._motion_controller
    assert first_hover is not None
    assert first_controller is not None

    # Second pass (e.g. setup_local_shell after construction) must replace,
    # not stack.
    backend.setup_link_handling(noop, noop, noop, noop)

    assert first_hover in backend.vte.disconnected
    assert first_controller in backend.vte.removed_controllers
    hover_signals = [
        name for name, _cb in backend.vte.signals.values()
        if name == "hyperlink-hover-uri-changed"
    ]
    assert hover_signals == ["hyperlink-hover-uri-changed"]
    assert len(backend.vte.controllers) - len(backend.vte.removed_controllers) == 1


# --------------------------------------------------------------------------
# TerminalWidget hover-state merge (unbound methods on a fake instance)
# --------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, plain_uri=None):
        self.plain_uri = plain_uri
        self.pointer_over_link = None

    def supports_feature(self, name):
        return True

    def plain_text_link_at(self, x, y):
        return self.plain_uri

    def hyperlink_at(self, x, y):
        raise AssertionError("motion path called the full probe")

    def set_pointer_over_link(self, over_link):
        self.pointer_over_link = over_link


class _FakeControllerMotion:
    def get_current_event(self):
        return object()


class _FakeTerminal:
    _on_vte_motion = TerminalWidget._on_vte_motion
    _on_vte_hyperlink_hover = TerminalWidget._on_vte_hyperlink_hover
    _update_hover_state = TerminalWidget._update_hover_state

    def __init__(self, plain_uri=None):
        self.backend = _FakeBackend(plain_uri)
        self._destroyed = False
        self._is_quitting = False
        self._hovered_osc8_uri = None
        self._hovered_plaintext_uri = None
        self._hovered_hyperlink_uri = None


def test_motion_uses_regex_probe_only_and_updates_cursor():
    term = _FakeTerminal(plain_uri="https://plain.example")
    term._on_vte_motion(_FakeControllerMotion(), 10, 10)
    assert term._hovered_plaintext_uri == "https://plain.example"
    assert term._hovered_hyperlink_uri == "https://plain.example"
    assert term.backend.pointer_over_link is True


def test_motion_regex_miss_does_not_clobber_osc8_hover():
    term = _FakeTerminal(plain_uri=None)
    term._on_vte_hyperlink_hover("https://osc8.example")
    assert term._hovered_hyperlink_uri == "https://osc8.example"
    assert term.backend.pointer_over_link is True

    # Pointer moves within the OSC 8 link: regex misses must not clear it.
    term._on_vte_motion(_FakeControllerMotion(), 12, 12)
    assert term._hovered_plaintext_uri is None
    assert term._hovered_hyperlink_uri == "https://osc8.example"
    assert term.backend.pointer_over_link is True


def test_osc8_signal_clearing_restores_text_cursor():
    term = _FakeTerminal(plain_uri=None)
    term._on_vte_hyperlink_hover("https://osc8.example")
    term._on_vte_hyperlink_hover(None)
    assert term._hovered_hyperlink_uri is None
    assert term.backend.pointer_over_link is False


def test_motion_and_hover_are_noops_after_destroy():
    term = _FakeTerminal(plain_uri="https://plain.example")
    term._destroyed = True
    term._on_vte_motion(_FakeControllerMotion(), 10, 10)
    term._on_vte_hyperlink_hover("https://osc8.example")
    assert term._hovered_hyperlink_uri is None
    assert term.backend.pointer_over_link is None
