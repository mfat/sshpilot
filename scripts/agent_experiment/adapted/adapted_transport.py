"""Adapted from cc04b976's hidden test: same assertions, but the test double
falls back to real TerminalWidget methods, so a fix that adds a helper method
is not rejected merely for being a method."""
from types import SimpleNamespace

from sshpilot.terminal import TerminalWidget


class _Double(SimpleNamespace):
    def __getattr__(self, name):
        attr = getattr(TerminalWidget, name)
        return attr.__get__(self) if callable(attr) else attr


def test_transport_closed_during_quit_is_not_a_connection_failure():
    from sshpilot.api.errors import ErrorCode, SshPilotError

    failures = []
    widget = _Double(_is_quitting=True, _on_connection_failed=lambda *a, **k: failures.append(a), _show_toast=lambda *_a, **_k: None)
    error = SshPilotError(ErrorCode.TRANSPORT_CLOSED, "The daemon transport is closed", retryable=True)
    TerminalWidget._on_daemon_error(widget, error)
    assert failures == []

    widget._is_quitting = False
    TerminalWidget._on_daemon_error(widget, error)
    assert failures  # outside quit it is still reported
