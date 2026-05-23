"""Tests for SSH port-forwarding command construction."""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple

import pytest

from sshpilot.connection_manager import Connection
import sshpilot.config as config_mod

asyncio.set_event_loop(asyncio.new_event_loop())


class _DummyProcess:
    def __init__(self):
        self.returncode = None
        self.stdout = None
        self.stderr = None

    async def wait(self) -> int:
        return 0


def _prepare_connection(monkeypatch: pytest.MonkeyPatch) -> Connection:
    class _ConfigStub:
        def get_ssh_config(self) -> dict:
            return {
                'keepalive_interval': 30,
                'keepalive_count_max': 3,
            }

    monkeypatch.setattr(config_mod, 'Config', _ConfigStub)
    conn = Connection({'host': 'fwd.example', 'hostname': 'fwd.example', 'username': 'alice'})
    loop = asyncio.get_event_loop()
    loop.run_until_complete(conn.connect())
    return conn


def _host_index(cmd: List[str]) -> int:
    idx = 1
    takes_value = {'-o', '-i', '-F', '-p', '-P', '-D', '-L', '-R'}
    while idx < len(cmd):
        token = cmd[idx]
        if token in takes_value and idx + 1 < len(cmd):
            idx += 2
            continue
        if token.startswith('-'):
            idx += 1
            continue
        return idx
    return -1


def test_build_forwarding_ssh_command_orders_local_forward_before_host():
    conn = Connection({'host': 'order.example', 'hostname': 'order.example'})
    cmd, env = conn._build_forwarding_ssh_command(
        ['-N', '-L', '127.0.0.1:8022:remote:22']
    )
    host_idx = _host_index(cmd)
    l_idx = cmd.index('-L')
    assert l_idx < host_idx
    assert cmd[0] == 'ssh'
    assert 'SSH_ASKPASS' not in env


def test_start_local_forwarding_uses_builder_not_stale_ssh_cmd(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _prepare_connection(monkeypatch)
    captured: List[Tuple[str, ...]] = []

    async def _fake_exec(*args: str, **kwargs: Any):
        captured.append(tuple(args))

        async def _communicate():
            return b'', b''

        proc = _DummyProcess()
        proc.communicate = _communicate  # type: ignore[method-assign]
        return proc

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', _fake_exec)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        conn.start_local_forwarding('127.0.0.1', 8022, 'internal', 22)
    )

    assert captured
    cmd = captured[0]
    host_idx = _host_index(list(cmd))
    l_idx = list(cmd).index('-L')
    assert l_idx < host_idx
    assert '127.0.0.1:8022:internal:22' in cmd
    assert 'ServerAliveInterval=30' in cmd


def test_start_remote_forwarding_uses_builder(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _prepare_connection(monkeypatch)
    captured: List[Tuple[str, ...]] = []

    async def _fake_exec(*args: str, **kwargs: Any):
        captured.append(tuple(args))

        async def _communicate():
            return b'', b''

        proc = _DummyProcess()
        proc.communicate = _communicate  # type: ignore[method-assign]
        return proc

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', _fake_exec)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        conn.start_remote_forwarding('0.0.0.0', 9000, 'db.internal', 5432)
    )

    assert captured
    cmd = list(captured[0])
    host_idx = _host_index(cmd)
    r_idx = cmd.index('-R')
    assert r_idx < host_idx


def test_dynamic_forwarding_places_dynamic_flag_before_host(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: List[Tuple[str, ...]] = []

    async def _fake_exec(*args: str, **kwargs: Any):
        captured.append(tuple(args))

        class _Proc(_DummyProcess):
            async def communicate(self):
                self.returncode = 0
                return b'', b''

            def terminate(self):
                return None

        return _Proc()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', _fake_exec)
    monkeypatch.setattr(
        config_mod,
        'Config',
        lambda: type('C', (), {'get_ssh_config': lambda self: {'batch_mode': True}})(),
    )

    conn = Connection({'host': 'socks.example', 'hostname': 'socks.example', 'username': 'u'})
    loop = asyncio.get_event_loop()
    loop.run_until_complete(conn.start_dynamic_forwarding('127.0.0.1', 9050))

    assert captured
    cmd = list(captured[0])
    host_idx = _host_index(cmd)
    d_idx = cmd.index('-D')
    assert d_idx < host_idx
    assert 'BatchMode=yes' in cmd
