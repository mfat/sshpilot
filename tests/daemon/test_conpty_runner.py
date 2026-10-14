"""ConPTY session runner, driven through a fake pseudo-console factory."""

from __future__ import annotations

import os
import queue
import threading
from types import SimpleNamespace

from sshpilot.api.models.terminal import TerminalDimensions
from sshpilot.daemon.conpty_runner import ConPtySessionProcessRunner


class FakePty:
    instances: list = []

    def __init__(self, cols, rows):
        self.size = (cols, rows)
        self.spawned = None
        self.written = []
        self.pid = None
        self._output = queue.Queue()
        self._alive = True
        self.exit_code = None
        FakePty.instances.append(self)

    def spawn(self, appname, cmdline=None, cwd=None, env=None):
        self.spawned = (appname, cmdline, cwd, env)
        return True

    def read(self, blocking=False):
        item = self._output.get()
        if item is None:
            raise EOFError
        return item

    def write(self, text):
        self.written.append(text)
        return len(text)

    def set_size(self, cols, rows):
        self.size = (cols, rows)

    def isalive(self):
        return self._alive

    def iseof(self):
        return not self._alive

    def get_exitstatus(self):
        return self.exit_code

    # test helpers
    def emit(self, text):
        self._output.put(text)

    def exit(self, code):
        self.exit_code = code
        self._alive = False
        self._output.put(None)


def _spec(rows=24, columns=80):
    return SimpleNamespace(
        session_id="session-1",
        connection_id="connection-1",
        dimensions=TerminalDimensions(rows=rows, columns=columns),
    )


def test_runner_streams_output_and_reports_exit(monkeypatch):
    FakePty.instances.clear()
    monkeypatch.setattr(
        "sshpilot.daemon.conpty_runner._resolve_executable",
        lambda command, env: "C:/Windows/System32/OpenSSH/ssh.exe",
    )
    runner = ConPtySessionProcessRunner(
        lambda spec: (["ssh", "-p", "22", "host name"], {"PATH": os.defpath}),
        pty_factory=FakePty,
    )
    output, exited, eof = [], threading.Event(), threading.Event()
    exit_info = {}
    try:
        handle = runner.start(
            _spec(rows=30, columns=100),
            lambda info: (exit_info.setdefault("info", info), exited.set()),
            output.append,
            eof.set,
        )
        pty = FakePty.instances[-1]
        assert pty.size == (100, 30)
        assert pty.spawned[0].endswith("ssh.exe")
        assert pty.spawned[1] == ' -p 22 "host name"'

        assert handle.write("é".encode("utf-8")[:1])  # split UTF-8 sequence
        assert handle.write("é".encode("utf-8")[1:])
        assert "".join(pty.written) == "é"

        handle.resize(TerminalDimensions(rows=40, columns=120))
        assert pty.size == (120, 40)

        pty.emit("hello\r\n")
        pty.exit(3)
        assert exited.wait(5)
        assert eof.is_set()
        assert b"".join(output) == b"hello\r\n"
        assert exit_info["info"].exit_code == 3
    finally:
        runner.close()
