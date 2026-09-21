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
    IdentityFailure,
    IdentityFailureCode,
    OperationKind,
    OperationState,
    OperationSummary,
)


def _summary(state="running", operation_id="op:1", message="", failure=None):
    return OperationSummary(
        operation_id=operation_id,
        kind=OperationKind.KEY_DEPLOYMENT,
        state=OperationState(state),
        message=message,
        created_at=datetime.fromtimestamp(0, tz=timezone.utc),
        failure=failure,
    )


def _window():
    window = MagicMock()
    window.client = MagicMock()
    window.client.deploy_key.return_value = _summary()
    window.client.get_operation.return_value = _summary()
    # No secrets-controller-gating in these tests - they exercise the deploy
    # RPC itself, not the vault-unlock gate (covered separately).
    window.terminal_manager = None
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
    window.terminal_manager = None
    deploy_key = window.client.deploy_key
    window.client = None
    runner = _runner(window)

    runner.run(_connection(), _key(), force=True)

    window._error_dialog.assert_called_once()
    deploy_key.assert_not_called()


def test_runner_requires_daemon_key_id():
    window = MagicMock()
    window.terminal_manager = None
    runner = _runner(window)

    runner.run(_connection(), SimpleNamespace(key_id=None))

    window._error_dialog.assert_called_once()
    window.client.deploy_key.assert_not_called()


def test_runner_submits_pasted_public_key_request():
    window = _window()
    runner = _runner(window)
    pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterialForUnitTest pasted@host"

    runner.run(_connection(), None, force=True, public_key=pub)

    window.client.deploy_key.assert_called_once()
    request = window.client.deploy_key.call_args[0][0]
    assert request.connection_id == "HostAlias"
    assert request.key_id is None
    assert request.public_key == pub
    assert request.force is True


def test_runner_rejects_empty_paste_without_key():
    window = _window()
    runner = _runner(window)

    runner.run(_connection(), None, public_key="   ")

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
        state="failed",
        operation_id="op:5",
        message="finalized daemon text must be ignored",
        failure=IdentityFailure(
            IdentityFailureCode.AUTHENTICATION_FAILED,
            ErrorCode.REMOTE_COMMAND_FAILED,
            diagnostic="Permission denied (publickey)",
        ),
    )
    callbacks = _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    window._error_dialog.assert_called_once()
    assert window._error_dialog.call_args[0][2] == (
        "Authentication failed while installing the public key\n\n"
        "Permission denied (publickey)"
    )
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
    assert window._error_dialog.call_args[0][2] == (
        "Public-key deployment is unavailable."
    )
    window.client.get_operation.assert_not_called()


def test_unknown_deployment_start_error_remains_opaque(monkeypatch):
    diagnostic = "daemon: opaque deployment failure"
    monkeypatch.setattr(
        win_mod,
        "_",
        lambda _msgid: pytest.fail("unknown diagnostics must not use gettext"),
    )

    assert win_mod._format_deployment_start_error(
        RuntimeError(diagnostic)
    ) == diagnostic


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


def test_runner_gates_on_vault_unlock(monkeypatch):
    """A locked session-backed vault must be unlocked before ssh-copy-id
    deploys - mirrors the gate used before opening a terminal/file-manager
    connection (TerminalManager._maybe_unlock_secrets_then)."""
    window = _window()
    runner = _runner(window)

    captured_retry = []
    terminal_manager = MagicMock()
    terminal_manager._maybe_unlock_secrets_then = MagicMock(
        side_effect=lambda retry: (captured_retry.append(retry), True)[1]
    )
    window.terminal_manager = terminal_manager

    runner.run(_connection(), _key(), force=True)

    # Gated: deployment must not have started yet.
    window.client.deploy_key.assert_not_called()
    assert len(captured_retry) == 1

    # Once the vault is unlocked, the retry must proceed without re-gating.
    captured_retry[0]()

    window.client.deploy_key.assert_called_once()
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()


# --- deployment progress -----------------------------------------------------
#
# The copy-key window closes the moment a deployment is handed off, so the
# operation used to run with nothing on screen until the result dialog. A
# pre-connection command -- a port knock or a VPN dial-up -- can hold that gap
# open for tens of seconds. The progress dialog fills it, and is bound to the
# operation id so the daemon can say what it is waiting on.


class _Recorder:
    """Captures what the runner does with the progress dialog and its binding."""

    def __init__(self):
        self.running_texts = []
        self.bound = []
        self.unbound = 0
        self.dialog = MagicMock()
        self.presented = []

    def present(self, parent, *, title, running_text, success_text, failure_text):
        self.presented.append((parent, title, running_text))
        return self.dialog, self.running_texts.append

    def bind(self, scope_id, setter):
        self.bound.append((scope_id, setter))

        def _unbind():
            self.unbound += 1

        return _unbind


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(win_mod, "present_operation_progress", rec.present)
    monkeypatch.setattr(win_mod, "bind_pre_command_status", rec.bind)
    return rec


def test_a_deployment_shows_progress_bound_to_its_operation(monkeypatch, recorder):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:11")
    _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    assert len(recorder.presented) == 1
    assert [scope for scope, _setter in recorder.bound] == ["op:11"]


def test_the_progress_dialog_is_dismissed_when_the_operation_ends(
    monkeypatch, recorder
):
    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:12")
    window.client.get_operation.return_value = _summary(
        state="succeeded", operation_id="op:12"
    )
    _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    recorder.dialog.close.assert_called_once()
    assert recorder.unbound == 1


def test_the_progress_dialog_is_dismissed_when_the_operation_fails(
    monkeypatch, recorder
):
    """A failed deployment must not leave a spinner running behind its error."""

    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:13")
    window.client.get_operation.return_value = _summary(
        state="failed",
        operation_id="op:13",
        failure=IdentityFailure(
            code=IdentityFailureCode.CONNECTION_REFUSED,
            error_code=ErrorCode.SESSION_STARTUP_FAILED,
        ),
    )
    _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    recorder.dialog.close.assert_called_once()
    assert recorder.unbound == 1
    window._error_dialog.assert_called_once()


def test_a_deployment_that_never_starts_shows_no_progress(monkeypatch, recorder):
    window = _window()
    window.client.deploy_key.side_effect = SshPilotError(
        ErrorCode.DAEMON_UNAVAILABLE, "no daemon"
    )
    _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())

    assert recorder.presented == []
    assert recorder.bound == []


def test_the_daemon_can_replace_the_running_text(monkeypatch, recorder):
    """This is the whole point: "running a port knock", not just "working"."""

    window = _window()
    window.client.deploy_key.return_value = _summary(operation_id="op:14")
    _capture_timeout(monkeypatch)
    runner = _runner(window)

    runner.run(_connection(), _key())
    _scope, setter = recorder.bound[0]
    setter("Running pre-connection command…")

    assert recorder.running_texts == ["Running pre-connection command…"]
