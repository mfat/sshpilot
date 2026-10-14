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

    def read(self, length=1000, blocking=False):
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


class StallingPty(FakePty):
    """pywinpty 2.x read semantics seen on Windows 10 ConPTY.

    A read returns at most 1000 characters. A blocking read waits for the
    console to write *again*, even when the last write is not fully read; a
    non-blocking read returns whatever is buffered.
    """

    def __init__(self, cols, rows):
        super().__init__(cols, rows)
        self._buffer = ""
        self._wrote = threading.Condition()
        self._writes = 0
        self._seen = 0

    def read(self, length=1000, blocking=False):
        with self._wrote:
            if blocking:
                while self._writes == self._seen:
                    self._wrote.wait()
                self._seen = self._writes
            if not self._alive and not self._buffer:
                raise EOFError
            chunk = self._buffer[: min(length, 1000)]
            self._buffer = self._buffer[len(chunk):]
            return chunk

    def emit(self, text):
        with self._wrote:
            self._buffer += text
            self._writes += 1
            self._wrote.notify_all()

    def exit(self, code):
        with self._wrote:
            self.exit_code = code
            self._alive = False
            self._writes += 1
            self._wrote.notify_all()


def test_runner_delivers_output_left_after_a_full_read(monkeypatch):
    """Output past one full read arrives without waiting for more output."""

    monkeypatch.setattr(
        "sshpilot.daemon.conpty_runner._resolve_executable",
        lambda command, env: "C:/Windows/System32/OpenSSH/ssh.exe",
    )
    runner = ConPtySessionProcessRunner(
        lambda spec: (["ssh", "host"], {"PATH": os.defpath}),
        pty_factory=StallingPty,
    )
    frame = "".join(f"{n}\r\n" for n in range(1, 401))  # seq 1 400
    received = []
    complete = threading.Event()

    def on_output(data):
        received.append(data)
        if len(b"".join(received)) == len(frame):
            complete.set()

    try:
        runner.start(_spec(), lambda info: None, on_output, lambda: None)
        pty = FakePty.instances[-1]
        pty.emit(frame)
        assert complete.wait(5), f"stalled after {len(b''.join(received))} bytes"
        assert b"".join(received) == frame.encode()
        pty.exit(0)
    finally:
        runner.close()
