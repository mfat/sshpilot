"""How the pre-connection command is actually executed.

Execution lives in ``terminal`` rather than ``terminal_manager`` because the
architecture guardrails allow only a short list of frontend modules to own a
local process, and ``terminal.py`` -- where this feature ran before the
daemon-backed refactor dropped it -- is on that list.
See tests/architecture/test_frontend_closure.py.
"""

import subprocess
from unittest import mock

from sshpilot.terminal import (
    PRE_CONNECTION_COMMAND_TIMEOUT,
    run_pre_connection_command,
)


def _captured(monkeypatch, result=None, raises=None):
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return result if result is not None else mock.Mock(returncode=0)

    monkeypatch.setattr("sshpilot.terminal.subprocess.run", _run)
    return calls


def test_runs_through_a_login_shell(monkeypatch):
    """A login shell sources the user's profile. That matters in the packaged
    app: a bundle launched from Finder inherits a minimal PATH without
    Homebrew, so a non-login shell would not find a knock helper at all."""
    calls = _captured(monkeypatch)

    run_pre_connection_command('fwknop -n app --wget-cmd "$(which wget)"')

    argv, kwargs = calls[0]
    assert argv[1] == "-lc"
    assert argv[2] == 'fwknop -n app --wget-cmd "$(which wget)"'
    assert argv[0].endswith("sh")
    # shell=True is banned across the frontend by the architecture guardrails;
    # argv with an explicit shell is how the sanctioned helpers do it.
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert kwargs["timeout"] == PRE_CONNECTION_COMMAND_TIMEOUT


def test_empty_command_does_not_spawn_anything(monkeypatch):
    calls = _captured(monkeypatch)

    run_pre_connection_command("")
    run_pre_connection_command("   ")
    run_pre_connection_command(None)

    assert calls == []


def test_non_zero_exit_is_swallowed(monkeypatch):
    """SSH's own error tells the user more than a pre-step veto would."""
    _captured(monkeypatch, result=mock.Mock(returncode=1))

    run_pre_connection_command("false")  # must not raise


def test_timeout_is_swallowed(monkeypatch):
    _captured(monkeypatch, raises=subprocess.TimeoutExpired("sh", 30))

    run_pre_connection_command("sleep 600")  # must not raise


def test_spawn_failure_is_swallowed(monkeypatch):
    _captured(monkeypatch, raises=OSError("no such executable"))

    run_pre_connection_command("nope")  # must not raise


def test_command_is_bounded_by_a_timeout(monkeypatch):
    """Unbounded, a hung knock helper would block the connection forever."""
    calls = _captured(monkeypatch)

    run_pre_connection_command("knock")

    assert calls[0][1]["timeout"] == 30
