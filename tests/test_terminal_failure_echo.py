"""How connection failures are written into the terminal itself.

A daemon session failure is shown in the banner only: ssh already printed its
diagnostic to the PTY, so echoing the classified reason into the terminal
repeated it. Any message that is still echoed must use CRLF, or a bare LF
leaves the cursor column in place and the next line staircases.
"""

from unittest.mock import Mock

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.sessions import SessionFailure, SessionFailureCode
from sshpilot.core.connection_evidence import (
    classify_connection_evidence,
    post_auth_exit_failure_reason,
)
from sshpilot.terminal_session_controller import (
    SessionFailureError,
    _session_failure_error,
)


@pytest.fixture
def terminal():
    from sshpilot.terminal import TerminalWidget

    widget = Mock()
    widget._fallback_timer_id = None
    widget.backend = Mock()
    widget.connection = None
    widget._classify_exit = Mock(return_value=(None, "Connection timed out"))
    widget._on_connection_failed = (
        lambda message, echo_to_terminal=True: TerminalWidget._on_connection_failed(
            widget, message, echo_to_terminal
        )
    )
    return widget


def _fed_text(widget) -> str:
    return b"".join(
        call.args[0] for call in widget.backend.feed.call_args_list
    ).decode("utf-8")


def test_multiline_error_is_echoed_with_crlf(terminal):
    from sshpilot.terminal import TerminalWidget

    TerminalWidget._on_connection_failed(terminal, "first line\n\nsecond line")

    fed = _fed_text(terminal)
    assert "first line\r\n\r\nsecond line" in fed
    assert "\n" not in fed.replace("\r\n", "")


def test_session_failure_is_not_echoed_but_still_bannered(terminal):
    from sshpilot.terminal import TerminalWidget

    failure = SessionFailure(
        code=SessionFailureCode.SSH_DIAGNOSTIC,
        error_code=ErrorCode.SESSION_STARTUP_FAILED,
        parameters={},
        diagnostic="ssh: connect to host 192.0.2.1 port 22: Connection timed out",
    )
    TerminalWidget._on_daemon_error(
        terminal, _session_failure_error(failure, "session-1")
    )

    terminal.backend.feed.assert_not_called()
    terminal._set_disconnected_banner_visible.assert_called_once()
    assert "Connection timed out" in terminal.last_error_message


def test_other_daemon_errors_are_still_echoed(terminal):
    from sshpilot.terminal import TerminalWidget

    TerminalWidget._on_daemon_error(
        terminal,
        SshPilotError(ErrorCode.SESSION_STARTUP_FAILED, "The daemon went away"),
    )

    assert "The daemon went away" in _fed_text(terminal)


def test_session_failure_error_is_an_sshpilot_error():
    failure = SessionFailure(
        code=SessionFailureCode.ENDED_BEFORE_OUTPUT,
        error_code=ErrorCode.SESSION_STARTUP_FAILED,
        parameters={},
        diagnostic="",
    )
    error = _session_failure_error(failure, "session-1")

    assert isinstance(error, SessionFailureError)
    assert isinstance(error, SshPilotError)
    assert error.code is ErrorCode.SESSION_STARTUP_FAILED
    assert error.session_id == "session-1"


def test_failure_reason_keeps_ssh_casing():
    text = "ssh: connect to host 192.0.2.1 port 22: Connection timed out\r\n"

    assert classify_connection_evidence(text).failure_reason == (
        "ssh: connect to host 192.0.2.1 port 22: Connection timed out"
    )
    assert post_auth_exit_failure_reason(text, exit_code=255) == (
        "ssh: connect to host 192.0.2.1 port 22: Connection timed out"
    )
