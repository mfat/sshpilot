"""Regression coverage for daemon error routing in the GTK client."""

from types import SimpleNamespace

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.window import MainWindow


def _window_with_dialog_spy():
    window = MainWindow.__new__(MainWindow)
    calls = []
    window._error_dialog = lambda heading, body, detail="": calls.append(
        (heading, body, detail)
    )
    return window, calls


def test_daemon_unavailable_dialog_accepts_zero_or_detail_arguments():
    window, calls = _window_with_dialog_spy()

    window._show_daemon_unavailable_dialog()
    window._show_daemon_unavailable_dialog("sessions are active")
    window._show_daemon_unavailable_dialog(
        SshPilotError(ErrorCode.TRANSPORT_TIMEOUT, "timed out")
    )

    assert calls[0][2] == ""
    assert calls[1][2] == "sessions are active"
    assert calls[2][2] == "timed out"


def test_startup_mode_transport_error_has_no_callback_type_error():
    window, calls = _window_with_dialog_spy()
    window._on_startup_operation_mode_error(
        SshPilotError(ErrorCode.TRANSPORT_CLOSED, "daemon socket closed")
    )

    assert calls and calls[-1][2] == "daemon socket closed"


def test_effective_config_error_does_not_route_through_external_terminal_handler():
    window, calls = _window_with_dialog_spy()
    external = []
    window._on_external_terminal_launch_error = external.append

    window._on_effective_config_error(
        SshPilotError(ErrorCode.CONNECTION_NOT_FOUND, "connection deleted")
    )
    window._on_effective_config_error(
        SshPilotError(ErrorCode.UNSUPPORTED_CAPABILITY, "comparison unsupported")
    )

    assert external == []
    assert len(calls) == 2
    assert all("effective" in call[0].lower() or "connection" in call[0].lower() for call in calls)


def test_effective_comparison_unavailable_is_not_reported_as_daemon_unavailable():
    window, calls = _window_with_dialog_spy()
    result = SimpleNamespace(available=False)

    window._present_effective_config_dialog("prod", result)

    assert len(calls) == 1
    assert calls[0][0] != "Daemon unavailable"


def test_operation_mode_recovery_has_explicit_user_facing_state():
    window, calls = _window_with_dialog_spy()

    window._show_operation_mode_recovery("runtime isolated; persisted default")

    assert calls[0][0] == "Operation mode recovery required"
    assert "runtime isolated" in calls[0][2]
