"""Daemon-backed public-key deployment runner (``SshCopyIdRunner``).

Deployment is a typed daemon operation: the runner submits a
``DeployKeyRequest`` to ``SshPilotClient.deploy_key``, then polls the
operation summary until it reaches a terminal state. GTK only presents the
outcome; it never launches ``ssh-copy-id`` itself.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest

pytest.importorskip("gi")

from sshpilot import sshcopyid_window as win_mod
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.operations import (
    OperationKind,
    OperationState,
    OperationSummary,
)


def _summary(state="running", operation_id="op:1", message=""):
    return OperationSummary(
        operation_id=operation_id,
        kind=OperationKind.KEY_DEPLOYMENT,
        state=OperationState(state),
        message=message,
        created_at=datetime.fromtimestamp(0, tz=timezone.utc),
    )


def _window():
    window = MagicMock()
    window.client = MagicMock()
    window.client.deploy_key.return_value = _summary()
    window.client.get_operation.return_value = _summary()
    return window


def _connection():
    return SimpleNamespace(nickname="HostAlias")


def _key():
    return SimpleNamespace(key_id="key:daemon-1")


def _runner(window):
    return win_mod.SshCopyIdRunner(window)


def _capture_timeout(monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        win_mod.GLib, "timeout_add", lambda ms, fn, *args: callbacks.append((ms, fn, args)) or 1
    )
    return callbacks


def test_runner_requires_daemon_client():
    window = MagicMock()
    deploy_key = window.client.deploy_key
    window.client = None
    runner = _runner(window)

    runner.run(_connection(), _key(), force=True)

    window._error_dialog.assert_called_once()
    deploy_key.assert_not_called()


def test_runner_requires_daemon_key_id():
    window = MagicMock()
    runner = _runner(window)

    runner.run(_connection(), SimpleNamespace(key_id=None))

    window._error_dialog.assert_called_once()
    window.client.deploy_key.assert_not_called()


def test_runner_submits_typed_deployment_request():
    window = _window()
    runner = _runner(window)

    runner.run(_connection(), _key(), force=True)

    window.client.deploy_key.assert_called_once()
    request = window.client.deploy_key.call_args[0][0]
    assert request.connection_id == "HostAlias"
    assert request.key_id == "key:daemon-1"
    assert request.force is True
    assert request.scope.value == "default"


def test_runner_polls_operation_until_terminal(monkeypatch):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:9")
    window.client.get_operation.return_value = _summary(state="succeeded", operation_id="op:9")
    callbacks = _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    window.client.get_operation.assert_called_once_with("op:9")
    assert callbacks == []
    window._info_dialog.assert_called_once()
    window._error_dialog.assert_not_called()


def test_runner_reports_deployment_failure(monkeypatch):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:5")
    window.client.get_operation.return_value = _summary(
        state="failed", operation_id="op:5", message="Authentication failed"
    )
    callbacks = _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    window._error_dialog.assert_called_once()
    window._info_dialog.assert_not_called()
    assert callbacks == []


def test_runner_reports_cancellation(monkeypatch):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:5")
    window.client.get_operation.return_value = _summary(
        state="cancelled", operation_id="op:5"
    )
    callbacks = _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    window._info_dialog.assert_called_once()
    window._error_dialog.assert_not_called()
    assert callbacks == []


def test_runner_start_failure_shows_error():
    window = _window()
    window.client.deploy_key.side_effect = SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY, "ssh-copy-id is unavailable"
    )
    runner = _runner(window)

    runner.run(_connection(), _key())

    window._error_dialog.assert_called_once()
    window.client.get_operation.assert_not_called()


def test_runner_continues_polling_while_running(monkeypatch):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:3")
    window.client.get_operation.side_effect = [
        _summary(state="running", operation_id="op:3"),
        _summary(state="succeeded", operation_id="op:3"),
    ]
    callbacks = _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())
    scheduled = callbacks[:]
    assert scheduled
    assert scheduled[0][0] == 250

    # The second poll observes the terminal state and stops rescheduling.
    callbacks.clear()
    scheduled[0][1](*scheduled[0][2])
    assert callbacks == []
    window._info_dialog.assert_called_once()


def test_runner_cancel_stops_polling(monkeypatch):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:7")
    window.client.get_operation.return_value = _summary(state="running", operation_id="op:7")
    callbacks = _capture_timeout(monkeypatch)
    runner = _runner(window)
    runner.run(_connection(), _key())

    source_removed = []
    monkeypatch.setattr(
        win_mod.GLib, "source_remove", lambda handle: source_removed.append(handle)
    )
    poll_id = callbacks[0]
    runner._poll_id = poll_id
    runner.cancel()

    window.client.cancel_operation.assert_called_once_with("op:7")
    assert source_removed == [poll_id]
    assert runner._operation_id is None
