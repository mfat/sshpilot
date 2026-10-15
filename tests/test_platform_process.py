from __future__ import annotations

import subprocess
import sys
import types

import pytest

from sshpilot.platform import process


def test_hidden_console_kwargs_follow_the_interpreter():
    expected = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else {}
    )
    assert process.hidden_console_kwargs() == expected


def test_hidden_console_kwargs_ignore_a_patched_platform(monkeypatch):
    # Tests patch sys.platform to drive Windows branches on Linux; POSIX
    # Popen would reject creationflags, so the helper must not follow it.
    monkeypatch.setattr(sys, "platform", "win32")
    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert process.hidden_console_kwargs() == {}


@pytest.mark.parametrize("on_error", [True, False, None])
def test_pid_alive_posix_errors_return_on_error(monkeypatch, on_error):
    monkeypatch.setattr(process.sys, "platform", "linux")

    def denied(pid, sig):
        raise PermissionError

    monkeypatch.setattr(process.os, "kill", denied)
    assert process.pid_alive(42, on_error=on_error) is on_error


def test_pid_alive_posix_dead_pid_is_false(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "linux")

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(process.os, "kill", gone)
    assert process.pid_alive(42, on_error=True) is False


def test_pid_alive_posix_live_pid_is_true(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert process.pid_alive(42, on_error=False) is True
    assert calls == [(42, 0)]


@pytest.mark.parametrize("exists", [True, False])
def test_pid_alive_windows_uses_psutil_not_os_kill(monkeypatch, exists):
    monkeypatch.setattr(process.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(pid_exists=lambda pid: exists))

    def must_not_kill(pid, sig):
        raise AssertionError("os.kill must not run on Windows")

    monkeypatch.setattr(process.os, "kill", must_not_kill)
    assert process.pid_alive(42, on_error=None) is exists


def test_pid_alive_windows_psutil_failure_returns_on_error(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "win32")

    def broken(pid):
        raise RuntimeError

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(pid_exists=broken))
    assert process.pid_alive(42, on_error=None) is None


def test_windows_cmd_command_line_keeps_inner_quotes(monkeypatch):
    monkeypatch.setenv("COMSPEC", r'"C:\Windows\system32\cmd.exe"')
    line = process.windows_cmd_command_line('echo "a b"')
    assert line == r'"C:\Windows\system32\cmd.exe" /d /s /c "echo "a b""'


def test_windows_cmd_command_line_defaults_to_cmd(monkeypatch):
    monkeypatch.delenv("COMSPEC", raising=False)
    assert process.windows_cmd_command_line("dir") == '"cmd.exe" /d /s /c "dir"'
