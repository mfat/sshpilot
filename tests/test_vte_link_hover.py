"""Regression coverage for VTE link delegation and issue #1104."""
import types
import pytest
pytest.importorskip("gi")
from sshpilot import terminal_backends
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import VTETerminalBackend


def _backend(**attrs):
    value = object.__new__(VTETerminalBackend)
    value.vte = types.SimpleNamespace(**attrs)
    value._destroyed = False
    return value


def test_one_shot_lookup_prefers_osc8_and_falls_back_to_match():
    calls = []
    backend = _backend(
        check_hyperlink_at=lambda x, y: calls.append(("osc8", x, y)),
        check_match_at=lambda x, y: (calls.append(("match", x, y)) or ("https://plain", 1)),
    )
    assert backend.hyperlink_at(4, 8) == "https://plain"
    assert calls == [("osc8", 4, 8), ("match", 4, 8)]

    backend.vte.check_hyperlink_at = lambda x, y: "https://osc8"
    assert backend.hyperlink_at(1, 2) == "https://osc8"


def test_repeated_motion_never_looks_up_vte_coordinates():
    class Backend:
        def hyperlink_at(self, *_args):
            raise AssertionError("motion performed a coordinate lookup")
        def plain_text_link_at(self, *_args):
            raise AssertionError("motion performed a match lookup")
    terminal = types.SimpleNamespace(backend=Backend())
    for position in range(100):
        TerminalWidget._on_vte_motion(terminal, object(), position, position)


class FakeVte:
    def __init__(self):
        self.matches = []
        self.cursor_names = []
        self.signals = []
    def match_add_regex(self, regex, flags):
        self.matches.append((regex, flags)); return 7
    def match_set_cursor_name(self, tag, name):
        self.cursor_names.append((tag, name))
    def connect(self, signal, callback):
        self.signals.append(signal); return len(self.signals)


def test_link_and_selection_setup_is_idempotent(monkeypatch):
    monkeypatch.setattr(terminal_backends.Vte, "Regex", types.SimpleNamespace(
        new_for_match=lambda *args: object()))
    backend = object.__new__(VTETerminalBackend)
    backend.vte = FakeVte()
    backend._link_match_tag = None
    backend._selection_handler = None
    noop = lambda *args: None
    backend.setup_link_handling(noop, noop, noop)
    backend.setup_link_handling(noop, noop, noop)
    assert len(backend.vte.matches) == 1
    assert backend.vte.cursor_names == [(7, "pointer")]
    assert backend.vte.signals == ["selection-changed"]


def test_context_and_click_queries_are_exact_one_shot():
    calls = []
    terminal = types.SimpleNamespace(
        _destroyed=False, _is_quitting=False,
        backend=types.SimpleNamespace(
            supports_feature=lambda name: name == "hyperlinks",
            hyperlink_at=lambda x, y: calls.append((x, y)) or "https://url"))
    assert TerminalWidget._vte_uri_at(terminal, 12, 34) == "https://url"
    assert calls == [(12, 34)]


def test_native_context_menu_handles_show_and_none_dismissal(monkeypatch):
    monkeypatch.setattr(terminal_backends.Vte, "EventContext", object, raising=False)
    class NativeVte:
        def __init__(self): self.callback = None; self.menus = []
        def set_context_menu(self, menu): self.menus.append(menu)
        def connect(self, signal, callback):
            assert signal == "setup-context-menu"; self.callback = callback; return 3
    backend = object.__new__(VTETerminalBackend)
    backend.vte = NativeVte()
    backend._native_context_handler = None
    lifecycle = []
    menu = object()
    assert backend.setup_native_context_menu(menu, lifecycle.append)
    context = object()
    backend.vte.callback(backend.vte, context)
    backend.vte.callback(backend.vte, None)
    assert backend.vte.menus == [menu]
    assert lifecycle == [True, False]
    backend.clear_native_context_menu()
    assert backend.vte.menus == [menu, None]
