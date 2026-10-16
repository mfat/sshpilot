"""``sshpilot <HostAlias>`` on a fresh launch waits for the daemon client.

The window is shown before the daemon answers, and alias lookup needs the
connection snapshot the daemon client brings. Resolving at once found no
saved alias, treated the destination as unsaved and opened it with no client:
"Daemon unavailable", and nothing opened. These pin the wait, and that a
destination resolved late still reports its status to whoever launched it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sshpilot.main import SshPilotApplication
from sshpilot.window import MainWindow


class _CommandLine:
    def __init__(self, argv, *, remote=False):
        self._argv = ["sshpilot", *argv]
        self._remote = remote
        self.stderr = []
        self.exit_status = None

    def get_arguments(self):
        return list(self._argv)

    def get_is_remote(self):
        return self._remote

    def printerr_literal(self, text):
        self.stderr.append(text)

    def set_exit_status(self, status):
        self.exit_status = status


class _Window:
    """Holds ready callbacks the way MainWindow does while selection is pending."""

    def __init__(self, *, connections=None, open_error=None):
        self.callbacks = []
        self.connection_manager = SimpleNamespace(connections=list(connections or []))
        self.opened = []
        self._open_error = open_error

    def present(self):
        pass

    def open_cli_connect(self, tokens):  # presence marks a ready window
        raise AssertionError("not used")

    def when_daemon_client_ready(self, callback):
        self.callbacks.append(callback)

    def open_cli_connect_resolved(self, resolved, sftp=False):
        if self._open_error is not None:
            raise self._open_error
        self.opened.append((resolved, sftp))

    def attach(self, ready=True):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback(ready)


@pytest.fixture
def app(monkeypatch):
    quits = []
    stub = SimpleNamespace(
        window=None,
        activate=lambda: None,
        quit=lambda: quits.append(True),
        quits=quits,
        _deferred_cli_command_lines=[],
        cli_exit_status=None,
    )
    stub._action_window = lambda: stub.window
    for name in ("do_command_line", "_cli_fail", "_open_cli_target", "_finish_deferred_cli"):
        setattr(stub, name, getattr(SshPilotApplication, name).__get__(stub))
    # Run idle callbacks inline: _cli_fail queues the quit on one.
    monkeypatch.setattr(
        "sshpilot.main.GLib.idle_add", lambda fn, *a: fn(*a), raising=False
    )
    return stub


def _resolve_to(monkeypatch, resolved="resolved"):
    seen = []

    def _resolve(tokens, manager):
        seen.append(list(manager.connections))
        return resolved

    monkeypatch.setattr("sshpilot.cli_connect.resolve_cli_connect", _resolve)
    return seen


def test_alias_is_resolved_only_after_the_snapshot_arrives(app, monkeypatch):
    seen = _resolve_to(monkeypatch)
    app.window = _Window()
    command_line = _CommandLine(["smoke"])

    assert app.do_command_line(command_line) == 0
    assert seen == [] and app.window.opened == []
    assert app._deferred_cli_command_lines == [command_line]

    app.window.connection_manager.connections.append("smoke")
    app.window.attach(True)

    assert seen == [["smoke"]]
    assert app.window.opened == [("resolved", False)]
    assert command_line.exit_status == 0
    assert app._deferred_cli_command_lines == []
    assert app.cli_exit_status is None


def test_ready_window_resolves_at_once(app, monkeypatch):
    _resolve_to(monkeypatch)
    window = _Window()
    window.when_daemon_client_ready = lambda callback: callback(True)
    app.window = window
    command_line = _CommandLine(["smoke"], remote=True)

    assert app.do_command_line(command_line) == 0
    assert window.opened == [("resolved", False)]
    assert app._deferred_cli_command_lines == []


def test_late_failure_reaches_the_exit_status(app, monkeypatch):
    _resolve_to(monkeypatch)
    app.window = _Window(open_error=RuntimeError("boom"))
    command_line = _CommandLine(["smoke"])

    assert app.do_command_line(command_line) == 0
    app.window.attach(True)

    assert command_line.stderr == ["sshpilot: boom\n"]
    assert command_line.exit_status == 255
    assert app.cli_exit_status == 255
    assert app.quits == [True]


def test_unavailable_daemon_fails_the_destination_but_keeps_the_window(app, monkeypatch):
    seen = _resolve_to(monkeypatch)
    app.window = _Window()
    command_line = _CommandLine(["smoke"])

    app.do_command_line(command_line)
    app.window.attach(False)

    assert seen == []
    assert command_line.stderr == [
        "sshpilot: the SSH Pilot background service is not available\n"
    ]
    assert app.cli_exit_status == 255
    # The window's recovery banner is how the user retries.
    assert app.quits == []


def test_remote_late_failure_is_not_the_primary_exit_status(app, monkeypatch):
    _resolve_to(monkeypatch)
    app.window = _Window(open_error=RuntimeError("boom"))
    command_line = _CommandLine(["smoke"], remote=True)

    app.do_command_line(command_line)
    app.window.attach(True)

    assert command_line.exit_status == 255
    assert app.cli_exit_status is None
    assert app.quits == []


def _window_stub(*, client=None, pending=True):
    window = SimpleNamespace(
        client=client,
        client_bridge=object(),
        _api_client_selection_pending=pending,
    )
    for name in ("when_daemon_client_ready", "_run_client_ready_callbacks"):
        setattr(window, name, getattr(MainWindow, name).__get__(window))
    return window


def test_window_runs_ready_callbacks_once():
    window = _window_stub()
    calls = []

    window.when_daemon_client_ready(calls.append)
    assert calls == []

    window._run_client_ready_callbacks(True)
    window._run_client_ready_callbacks(True)
    assert calls == [True]


def test_window_answers_at_once_when_attached_or_failed():
    attached = _window_stub(client=object())
    failed = _window_stub(pending=False)
    calls = []

    attached.when_daemon_client_ready(calls.append)
    failed.when_daemon_client_ready(calls.append)

    assert calls == [True, False]
