"""TerminalWidget._classify_exit: clearer message when a saved password fails.

When sshPilot fed a stored password (sshpass) and the server still denied the
connection, the disconnect banner should say the saved password was rejected
rather than a generic "Authentication failed", so the user knows to fix it.
"""
import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget
from sshpilot.connection_manager import ConnectionState


def _term(*, used_stored_password):
    t = TerminalWidget.__new__(TerminalWidget)
    t.last_error_message = ""
    t._connect_failure_hint = ""
    t._used_stored_password = used_stored_password
    return t


def test_saved_password_rejected_when_stored_password_used():
    t = _term(used_stored_password=True)
    state, reason = t._classify_exit(
        5, was_connected=False, extra_text="Permission denied, please try again."
    )
    assert state == ConnectionState.FAILED
    assert reason == "Saved password rejected"


def test_generic_auth_failed_without_stored_password():
    t = _term(used_stored_password=False)
    state, reason = t._classify_exit(
        255, was_connected=False, extra_text="Permission denied (publickey)."
    )
    assert state == ConnectionState.FAILED
    assert reason == "Authentication failed"


def test_too_many_failures_not_attributed_to_password():
    # "too many authentication failures" is about offered keys, not the password.
    t = _term(used_stored_password=True)
    state, reason = t._classify_exit(
        255, was_connected=False,
        extra_text="Received disconnect: Too many authentication failures",
    )
    assert state == ConnectionState.FAILED
    assert reason == "Authentication failed"


def test_missing_flag_defaults_to_generic_message():
    # Defensive: attribute absent (older code path) -> generic message, no crash.
    t = TerminalWidget.__new__(TerminalWidget)
    t.last_error_message = ""
    t._connect_failure_hint = ""
    state, reason = t._classify_exit(
        5, was_connected=False, extra_text="Permission denied, please try again."
    )
    assert reason == "Authentication failed"
