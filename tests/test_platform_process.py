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


class _FakePsutilError(Exception):
    pass


class _FakeNoSuchProcess(_FakePsutilError):
    pass


class _FakeAccessDenied(_FakePsutilError):
    pass


def _fake_psutil(tree, *, kill_fails=(), lookup_error=None, waited=None):
    """psutil stand-in: *tree* maps a pid to its descendants."""
    killed = []

    class Proc:
        def __init__(self, pid):
            if lookup_error is not None:
                raise lookup_error
            if pid not in tree and not any(pid in kids for kids in tree.values()):
                raise _FakeNoSuchProcess()
            self.pid = pid

        def children(self, recursive=False):
            return [Proc(child) for child in tree.get(self.pid, ())]

        def kill(self):
            if self.pid in kill_fails:
                raise _FakeAccessDenied()
            killed.append(self.pid)

    def wait_procs(procs, timeout=None):
        if waited is not None:
            waited.append((sorted(p.pid for p in procs), timeout))
        return [], []

    module = types.SimpleNamespace(
        Process=Proc,
        Error=_FakePsutilError,
        NoSuchProcess=_FakeNoSuchProcess,
        wait_procs=wait_procs,
    )
    return module, killed


def test_kill_process_tree_kills_children_before_parent(monkeypatch):
    psutil, killed = _fake_psutil({10: (11, 12)})
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    assert process.kill_process_tree(10) is True
    assert killed == [11, 12, 10]


def test_kill_process_tree_continues_past_a_member_it_cannot_kill(monkeypatch):
    psutil, killed = _fake_psutil({10: (11, 12)}, kill_fails={11})
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    assert process.kill_process_tree(10) is True
    assert killed == [12, 10]


def test_kill_process_tree_gone_pid_is_success(monkeypatch):
    psutil, killed = _fake_psutil({})
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    assert process.kill_process_tree(10) is True
    assert killed == []


def test_kill_process_tree_uninspectable_pid_is_failure(monkeypatch):
    psutil, killed = _fake_psutil({10: ()}, lookup_error=_FakeAccessDenied())
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    assert process.kill_process_tree(10) is False
    assert killed == []


def test_kill_process_tree_waits_only_when_asked(monkeypatch):
    waited = []
    psutil, _ = _fake_psutil({10: (11,)}, waited=waited)
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    process.kill_process_tree(10)
    assert waited == []
    process.kill_process_tree(10, wait_timeout=2)
    assert waited == [([10, 11], 2)]
