"""Regression tests for TerminalManager's vault-unlock-before-connect gate.

The SecretBackendsController rejects overlapping guarded operations with a
plain RuntimeError("...already in progress"). The startup vault-unlock
worker (secret_unlock_dialog.unlock_at_startup) can hold the controller for
as long as the user takes to answer the master-password prompt. If a
connection is started during that window, the old single-shot
controller.load_state() call in _maybe_unlock_secrets_then treated that
RuntimeError identically to "no unlock needed" and silently skipped the
vault-unlock prompt, letting SSH's own askpass fire for the host instead.
"""

from types import SimpleNamespace
from unittest import mock

from sshpilot.terminal_manager import TerminalManager


class _SyncThread:
    """Runs the thread target synchronously so tests don't need real threads."""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _install_sync_harness(monkeypatch):
    import sshpilot.terminal_manager as tm

    monkeypatch.setattr(tm.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        tm.GLib, "idle_add",
        lambda callback, *args: (callback(*args), False)[1],
    )
    monkeypatch.setattr(tm.time, "sleep", lambda _seconds: None)


def _manager():
    window = mock.Mock()
    return TerminalManager(window)


def test_load_secret_state_with_retry_rides_out_busy_controller(monkeypatch):
    import sshpilot.terminal_manager as tm

    monkeypatch.setattr(tm.time, "sleep", lambda _seconds: None)
    manager = _manager()

    controller = mock.Mock()
    calls = []

    def load_state():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("a secret backend operation is already in progress")
        return mock.Mock(needs_unlock=True, login_required=False)

    controller.load_state.side_effect = load_state

    state = manager._load_secret_state_with_retry(controller)

    assert state is not None
    assert state.needs_unlock is True
    assert len(calls) == 3


def test_load_secret_state_with_retry_gives_up_after_bounded_retries(monkeypatch):
    import sshpilot.terminal_manager as tm

    monkeypatch.setattr(tm.time, "sleep", lambda _seconds: None)
    manager = _manager()

    controller = mock.Mock()
    controller.load_state.side_effect = RuntimeError(
        "a secret backend operation is already in progress"
    )

    state = manager._load_secret_state_with_retry(controller)

    assert state is None
    assert controller.load_state.call_count == 40


def test_maybe_unlock_secrets_then_no_controller_proceeds_immediately():
    manager = _manager()
    manager.window.secrets_controller = None

    retried = []
    result = manager._maybe_unlock_secrets_then(lambda: retried.append(1))

    assert result is False
    assert retried == []


def test_maybe_unlock_secrets_then_rides_out_busy_controller_and_still_prompts(monkeypatch):
    """A transient busy controller (racing the startup unlock) must not be
    mistaken for "no unlock needed" - the unlock prompt must still show
    once the controller frees up, instead of connecting straight to SSH."""
    _install_sync_harness(monkeypatch)
    manager = _manager()

    controller = mock.Mock()
    calls = []

    def load_state():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("a secret backend operation is already in progress")
        return mock.Mock(needs_unlock=True, login_required=False)

    controller.load_state.side_effect = load_state
    manager.window.secrets_controller = controller

    prompt_calls = []

    def fake_prompt_unlock(window, on_done=None):
        prompt_calls.append(window)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_unlock_dialog.prompt_unlock", fake_prompt_unlock
    )

    retried = []
    result = manager._maybe_unlock_secrets_then(lambda: retried.append(1))

    assert result is True
    # The busy retries must have been ridden out, then the unlock prompt
    # shown - not silently treated as "proceed with the SSH connection".
    assert prompt_calls == [manager.window]
    assert retried == []


def test_maybe_unlock_secrets_then_proceeds_when_state_says_unlocked(monkeypatch):
    _install_sync_harness(monkeypatch)
    manager = _manager()

    controller = mock.Mock()
    controller.load_state.return_value = mock.Mock(
        needs_unlock=False, login_required=False
    )
    manager.window.secrets_controller = controller

    retried = []
    result = manager._maybe_unlock_secrets_then(lambda: retried.append(1))

    assert result is True
    assert retried == [1]


def test_create_terminal_for_pane_gates_on_vault_unlock(monkeypatch):
    """Split-pane creation has its own daemon-connect call, separate from
    connect_to_host's gated path - it must be gated too, or a locked vault
    falls through to a raw host-password prompt there instead."""
    manager = _manager()
    window = manager.window
    window.config = mock.Mock()
    window.connection_manager = mock.Mock()
    window.connection_to_terminals = {}
    window.terminal_to_connection = {}
    window.active_terminals = {}
    window.group_manager = None

    monkeypatch.setattr(
        manager, "_ensure_daemon_terminal_ready", lambda: mock.Mock(ready=True)
    )
    fake_terminal = mock.Mock()
    # Patch the exact globals dict this method's code object reads from
    # (rather than the module attribute) so this is immune to any other
    # test in the same worker process reloading sshpilot.terminal_manager.
    method_globals = TerminalManager.create_terminal_for_pane.__globals__
    monkeypatch.setitem(
        method_globals, "TerminalWidget", mock.Mock(return_value=fake_terminal)
    )

    idle_callbacks = []
    monkeypatch.setitem(
        method_globals, "GLib",
        SimpleNamespace(idle_add=lambda cb, *a: (idle_callbacks.append(cb), 1)[1]),
    )

    captured_retry = []
    monkeypatch.setattr(
        manager, "_maybe_unlock_secrets_then",
        lambda retry: (captured_retry.append(retry), True)[1],
    )

    connection = mock.Mock(nickname="TestHost")
    manager.create_terminal_for_pane(connection)

    assert len(idle_callbacks) == 1
    idle_callbacks[0]()

    # Gated: the daemon session must not have started yet.
    fake_terminal.start_daemon_session.assert_not_called()
    assert len(captured_retry) == 1

    # Once the vault is unlocked, the retry must proceed without re-gating.
    captured_retry[0]()
    fake_terminal.start_daemon_session.assert_called_once()


def test_reconnect_terminal_gated_gates_on_vault_unlock():
    """A dropped connection can outlive the vault's own lock (e.g. an idle
    timeout firing while disconnected) - reconnecting must re-check it the
    same way a fresh connect does."""
    manager = _manager()

    captured_retry = []
    manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (captured_retry.append(retry), True)[1]
    )
    manager.reconnect_terminal = mock.Mock(return_value=True)

    terminal = mock.Mock()
    result = manager._reconnect_terminal_gated(terminal)

    assert result is True
    manager.reconnect_terminal.assert_not_called()
    assert len(captured_retry) == 1

    captured_retry[0]()
    manager.reconnect_terminal.assert_called_once_with(terminal)


def test_reconnect_terminal_gated_reports_failure_via_terminal_banner():
    """When the gated retry's underlying reconnect fails, the terminal must
    get the same failure feedback the manual reconnect-banner click used to
    apply directly (overlay off, error recorded, banner shown)."""
    manager = _manager()
    manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (retry(), True)[1]
    )
    manager.reconnect_terminal = mock.Mock(return_value=False)

    terminal = mock.Mock()
    manager._reconnect_terminal_gated(terminal)

    terminal._set_connecting_overlay_visible.assert_called_once_with(False)
    terminal._record_error_detail.assert_called_once()
    terminal._set_disconnected_banner_visible.assert_called_once_with(
        True, mock.ANY
    )


def test_reconnect_terminal_gated_falls_through_without_controller():
    manager = _manager()
    manager.window.secrets_controller = None
    manager.reconnect_terminal = mock.Mock(return_value=True)

    terminal = mock.Mock()
    result = manager._reconnect_terminal_gated(terminal)

    assert result is True
    manager.reconnect_terminal.assert_called_once_with(terminal)
