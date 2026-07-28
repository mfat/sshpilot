from types import SimpleNamespace

import pytest

from sshpilot.api import InProcessClient
from sshpilot.api.client_factory import (
    CLIENT_MODE_ENVIRONMENT,
    ClientFallbackReason,
    ClientMode,
    parse_client_mode,
    requested_client_mode,
    select_client,
)
from sshpilot.daemon.launcher import (
    DaemonLaunchError,
    DaemonLaunchResult,
    DaemonStartupFailure,
)


class _Manager:
    def get_connections(self):
        return []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ClientMode.IN_PROCESS),
        ("", ClientMode.IN_PROCESS),
        ("  ", ClientMode.IN_PROCESS),
        ("in_process", ClientMode.IN_PROCESS),
        (" In_Process ", ClientMode.IN_PROCESS),
        ("daemon", ClientMode.DAEMON),
        ("\tDAEMON\n", ClientMode.DAEMON),
    ],
)
def test_client_mode_parser_has_documented_case_and_whitespace_policy(
    value,
    expected,
):
    assert parse_client_mode(value) is expected


def test_invalid_client_mode_is_rejected_safely():
    with pytest.raises(ValueError, match="invalid SSH Pilot client mode"):
        parse_client_mode("automatic")


def test_environment_mode_defaults_to_in_process():
    assert requested_client_mode({}) is ClientMode.IN_PROCESS
    assert requested_client_mode(
        {CLIENT_MODE_ENVIRONMENT: "daemon"}
    ) is ClientMode.DAEMON


def test_default_selection_does_not_touch_daemon_launcher():
    class _ForbiddenLauncher:
        def connect_or_start(self):
            raise AssertionError("default mode must not probe or launch a daemon")

    selection = select_client(
        _Manager(),
        mode=ClientMode.IN_PROCESS,
        launcher=_ForbiddenLauncher(),
    )

    assert selection.mode is ClientMode.IN_PROCESS
    assert isinstance(selection.client, InProcessClient)
    assert selection.daemon_process is None
    assert selection.warning is None


def test_daemon_selection_uses_launcher_result():
    client = SimpleNamespace(close=lambda: None)
    process = SimpleNamespace(started_by_frontend=True)
    launcher = SimpleNamespace(
        connect_or_start=lambda: DaemonLaunchResult(client, process)
    )

    selection = select_client(
        _Manager(),
        mode=ClientMode.DAEMON,
        launcher=launcher,
    )

    assert selection.mode is ClientMode.DAEMON
    assert selection.client is client
    assert selection.daemon_process is process
    assert selection.fallback_reason is None


def test_daemon_failure_falls_back_once_with_safe_message(caplog):
    secret = "token=must-not-appear"

    class _FailingLauncher:
        calls = 0

        def connect_or_start(self):
            self.calls += 1
            error = DaemonLaunchError(DaemonStartupFailure.STARTUP_TIMEOUT)
            error.__cause__ = RuntimeError(secret)
            raise error

    launcher = _FailingLauncher()
    fallback = InProcessClient(_Manager())
    selection = select_client(
        _Manager(),
        mode=ClientMode.DAEMON,
        in_process_client=fallback,
        launcher=launcher,
    )

    assert launcher.calls == 1
    assert selection.mode is ClientMode.IN_PROCESS
    assert selection.client is fallback
    assert selection.fallback_reason is ClientFallbackReason.DAEMON_STARTUP_FAILED
    assert "compatibility mode" in selection.warning
    assert secret not in selection.warning
    assert secret not in caplog.text
