"""Regression coverage for VTE search regex compilation."""
import types

import pytest

pytest.importorskip("gi")

from sshpilot import terminal_backends
from sshpilot.terminal_backends import VTETerminalBackend


def test_search_query_compiles_regex_with_multiline_flag(monkeypatch):
    regex_calls = []

    class FakeRegex:
        @staticmethod
        def new_for_search(*args):
            regex_calls.append(args)
            return object()

    monkeypatch.setattr(terminal_backends.Vte, "Regex", FakeRegex)
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(
        search_set_regex=lambda regex, flags: None,
        search_set_wrap_around=lambda enabled: None,
    )

    backend.search_set_query("a+b*", case_sensitive=False, regex=False)

    assert regex_calls == [
        (
            "(?i)a\\+b\\*",
            -1,
            0x00000400,
        )
    ]
