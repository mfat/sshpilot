"""The frontend's local-shell PTY runner for the built-in terminal backend."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from sshpilot.local_shell_pty import LocalShellProcess, windows_command_line


@pytest.mark.parametrize(
    "arguments",
    [
        ["-NoLogo"],
        ["a b", "c"],
        ['x"y'],
        ["C:\\a b\\"],
        ["", "z"],
        ['back\\\\"q'],
        ["tab\there"],
    ],
)
def test_windows_quoting_matches_list2cmdline(arguments):
    assert windows_command_line(arguments) == subprocess.list2cmdline(arguments)


def _run(argv, *, write=None, env=None, cwd=None, timeout=20):
    output = bytearray()
    exited = threading.Event()
    status = []

    def on_exit(code):
        status.append(code)
        exited.set()

    process = LocalShellProcess(output.extend, on_exit)
    process.start(argv, env=env, cwd=cwd, columns=100, rows=30)
    assert process.pid and process.is_running() in (True, False)
    if write is not None:
        process.write(write)
    assert exited.wait(timeout), bytes(output)
    process.close()
    return bytes(output), status[0], process


def _shell_command(script_posix: str, script_cmd: str):
    if sys.platform == "win32":
        comspec = os.environ.get("ComSpec") or os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
        )
        return [comspec, "/d", "/c", script_cmd]
    return ["/bin/sh", "-c", script_posix]


def test_output_then_exit_status():
    output, status, process = _run(
        _shell_command("printf 'hello-local\\n'; exit 3", "echo hello-local& exit /b 3")
    )
    assert b"hello-local" in output
    assert status == 3
    assert not process.is_running()


def test_cwd_and_environment_reach_the_shell(tmp_path):
    env = dict(os.environ, SSHPILOT_LOCAL_SHELL_PROBE="probe-value")
    output, status, _process = _run(
        _shell_command(
            "printf '%s|' \"$SSHPILOT_LOCAL_SHELL_PROBE\"; pwd",
            "echo %SSHPILOT_LOCAL_SHELL_PROBE%^|%CD%",
        ),
        env=env,
        cwd=str(tmp_path),
    )
    text = output.decode("utf-8", "replace")
    assert "probe-value" in text
    # Without the bundled conpty.dll (vendor/ is untracked, so a fresh
    # worktree has none) the inbox conhost hard-wraps the echoed path at the
    # 100-column width, splitting the directory name across a CR LF.
    unwrapped = text.replace("\r", "").replace("\n", "")
    assert os.path.basename(str(tmp_path)).lower() in unwrapped.lower()
    assert status == 0


def test_input_reaches_the_shell():
    if sys.platform == "win32":
        argv = [os.environ.get("ComSpec") or "cmd.exe", "/d", "/q", "/k"]
        line = b"echo typed-%OS%& exit\r"
    else:
        argv = ["/bin/sh"]
        line = b"echo typed-$((40+2)); exit\n"
    output, _status, _process = _run(argv, write=line)
    assert (b"typed-Windows_NT" if sys.platform == "win32" else b"typed-42") in output


def test_terminate_ends_a_running_shell():
    argv = (
        [os.environ.get("ComSpec") or "cmd.exe", "/d", "/q", "/k"]
        if sys.platform == "win32"
        else ["/bin/sh", "-c", "sleep 60"]
    )
    exited = threading.Event()
    process = LocalShellProcess(lambda _data: None, lambda _code: exited.set())
    process.start(argv, columns=80, rows=24)
    assert process.is_running()
    process.terminate()
    assert exited.wait(20)
    process.close()
    assert not process.is_running()
