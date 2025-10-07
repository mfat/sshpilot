import asyncio
from typing import Any, List, Tuple

import pytest

from sshpilot.connection_manager import Connection
import sshpilot.config as config_mod


# Ensure a dedicated event loop for Connection instances in this module
asyncio.set_event_loop(asyncio.new_event_loop())


class _DummyStream:
    async def read(self, *args: Any, **kwargs: Any) -> bytes:  # pragma: no cover - interface shim
        return b""


class _DummyProcess:
    def __init__(self):
        self.returncode = None
        self.stdout = _DummyStream()
        self.stderr = _DummyStream()

    async def wait(self) -> int:  # pragma: no cover - deterministic return
        return 0


def _prepare_connection(monkeypatch: pytest.MonkeyPatch, interval: int = 45, count: int = 6) -> Connection:
    class _ConfigStub:
        def get_ssh_config(self) -> dict:
            return {
                'keepalive_interval': interval,
                'keepalive_count_max': count,
            }

    monkeypatch.setattr(config_mod, 'Config', _ConfigStub)

    conn = Connection({'host': 'example.com', 'username': 'alice'})
    loop = asyncio.get_event_loop()
    loop.run_until_complete(conn.connect())
    return conn


def test_connection_appends_keepalive_options(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _prepare_connection(monkeypatch)
    cmd = conn.ssh_cmd

    assert 'ServerAliveInterval=45' in cmd
    assert 'ServerAliveCountMax=6' in cmd

    interval_index = cmd.index('ServerAliveInterval=45')
    count_index = cmd.index('ServerAliveCountMax=6')

    assert cmd[interval_index - 1] == '-o'
    assert cmd[count_index - 1] == '-o'


def test_port_forwarding_inherits_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _prepare_connection(monkeypatch)
    captured: List[Tuple[str, ...]] = []

    async def _fake_create_subprocess_exec(*args: str, **kwargs: Any):
        captured.append(tuple(args))
        return _DummyProcess()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', _fake_create_subprocess_exec)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        conn.start_local_forwarding('127.0.0.1', 8022, 'remote.host', 22)
    )
    loop.run_until_complete(
        conn.start_remote_forwarding('0.0.0.0', 9000, 'remote.host', 22)
    )

    assert captured, "Expected subprocess calls for forwarding rules"
    for cmd in captured:
        assert 'ServerAliveInterval=45' in cmd
        assert 'ServerAliveCountMax=6' in cmd
