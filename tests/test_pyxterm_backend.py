"""Tests for the PyXtermTerminalBackend command handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import subprocess

import pytest

from sshpilot.terminal_backends import PyXtermTerminalBackend


class _DummyProcess:
    def __init__(self, command: List[str]) -> None:
        self.command = command
        self.pid = 123

    def terminate(self) -> None:  # pragma: no cover - behaviour tested via destroy
        pass

    def wait(self, timeout: float | None = None) -> None:  # pragma: no cover - behaviour tested via destroy
        pass

    def kill(self) -> None:  # pragma: no cover - behaviour tested via destroy
        pass


class _DummyWebView:
    def __init__(self) -> None:
        self.loaded: list[str] = []

    def load_uri(self, uri: str) -> None:
        self.loaded.append(uri)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> PyXtermTerminalBackend:
    backend = object.__new__(PyXtermTerminalBackend)
    backend.owner = None
    backend.available = True
    backend._pyxterm = object()
    backend._webview = _DummyWebView()
    backend.widget = object()
    backend._server = None
    backend._terminal_id = None
    backend._child_pid = None
    backend._temp_script_path = None
    backend._server_process = None

    popen_calls: Dict[str, Any] = {}

    def fake_popen(cmd: List[str], stdout: Any = None, stderr: Any = None, env: Any = None) -> _DummyProcess:
        popen_calls["cmd"] = cmd
        process = _DummyProcess(cmd)
        popen_calls["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    import time

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    backend._popen_calls = popen_calls  # type: ignore[attr-defined]
    return backend  # type: ignore[return-value]


def test_spawn_async_uses_helper_script_for_sshpass_with_spaces(backend: PyXtermTerminalBackend) -> None:
    command = [
        "sshpass",
        "-p",
        "top secret",
        "ssh",
        "-o",
        "ProxyCommand=nc relay \"foo bar\"",
        "user@example.com",
    ]

    backend.spawn_async(command)

    cmd: List[str] = backend._popen_calls["cmd"]  # type: ignore[index]

    assert "--cmd-args" not in cmd

    assert "--command" in cmd
    script_index = cmd.index("--command") + 1
    script_path = Path(cmd[script_index])

    assert script_path.exists()

    script_contents = script_path.read_text()
    assert "exec sshpass" in script_contents
    assert "'top secret'" in script_contents
    assert "'ProxyCommand=nc relay \"foo bar\"'" in script_contents

    backend.destroy()
    assert backend._temp_script_path is None
    assert not script_path.exists()
