"""In a frozen (PyInstaller) build, sys.executable is the application binary
itself, not a Python interpreter. PtySessionProcessRunner.start() must not
spawn (sys.executable, _pty_child.py path, ...) in that case — the OS would
just relaunch the GUI, which is exactly what caused GH #1166 (ssh never
actually starts, and the session exits ~instantly with no PTY output at
all). It must instead re-invoke the frozen binary with the internal dispatch
flag that run.py handles, mirroring the precedent already established for
the daemon launch itself (see test_launcher.py's frozen-dispatch test)."""

import sys

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.daemon.pty_runner import PtySessionProcessRunner
from sshpilot.daemon.session_runtime import SessionLaunchSpec


def _spec():
    return SessionLaunchSpec(
        session_id=SessionId("session-1"),
        connection_id=ConnectionId("conn-1"),
        protocol="ssh",
        hostname="example.test",
        username="alice",
        port=22,
    )


def test_frozen_build_spawns_internal_pty_child_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/fake/SSHPilot", raising=False)

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        # Abort before an actual process exists; the test only cares about
        # the argv PtySessionProcessRunner decided to launch.
        raise OSError("stop before spawning for real")

    monkeypatch.setattr("sshpilot.daemon.pty_runner.subprocess.Popen", fake_popen)

    runner = PtySessionProcessRunner(lambda _spec: (("ssh", "-F", "cfg", "host"), {}))
    try:
        with pytest.raises(SshPilotError) as raised:
            runner.start(_spec(), lambda _info: None, lambda _data: None, lambda: None)
        assert raised.value.code is ErrorCode.PTY_ALLOCATION_FAILED
    finally:
        runner.close()

    command = captured["command"]
    assert command[0] == "/fake/SSHPilot"
    assert command[1] == "--internal-pty-child"
    assert command[4] == "--"
    assert command[5:] == ("ssh", "-F", "cfg", "host")
    # No .py helper path anywhere in the command — the frozen dispatch never
    # tries to run a script file as if the binary were a Python interpreter.
    assert not any(str(part).endswith(".py") for part in command)


def test_non_frozen_build_still_spawns_pty_child_script(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        raise OSError("stop before spawning for real")

    monkeypatch.setattr("sshpilot.daemon.pty_runner.subprocess.Popen", fake_popen)

    runner = PtySessionProcessRunner(lambda _spec: (("ssh", "-F", "cfg", "host"), {}))
    try:
        with pytest.raises(SshPilotError):
            runner.start(_spec(), lambda _info: None, lambda _data: None, lambda: None)
    finally:
        runner.close()

    command = captured["command"]
    assert command[0] == sys.executable
    assert command[1].endswith("_pty_child.py")
    assert command[4] == "--"
    assert command[5:] == ("ssh", "-F", "cfg", "host")
