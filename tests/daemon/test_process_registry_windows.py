"""PID liveness on Windows must never go through ``os.kill(pid, 0)``.

On Windows ``os.kill`` with any non-CTRL signal is ``TerminateProcess``, and a
dead PID raises a plain ``OSError`` that the POSIX branch reads as "alive" --
which kept quit waiting on a daemon that had already exited.
"""

from __future__ import annotations

import os
import types

import pytest

from sshpilot.daemon import process_registry


@pytest.mark.parametrize("exists", [True, False])
def test_windows_pid_exists_uses_psutil_not_os_kill(monkeypatch, exists):
    monkeypatch.setattr(__import__("sys"), "platform", "win32")
    fake_psutil = types.SimpleNamespace(pid_exists=lambda pid: exists)
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)

    def _forbidden(*_args):
        raise AssertionError("os.kill must not be used to probe on Windows")

    monkeypatch.setattr(os, "kill", _forbidden)
    assert process_registry._pid_exists(4242) is exists
