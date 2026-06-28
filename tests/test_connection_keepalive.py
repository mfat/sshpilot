import asyncio

import pytest

from sshpilot.connection_manager import Connection
import sshpilot.config as config_mod


# Ensure a dedicated event loop for Connection instances in this module
asyncio.set_event_loop(asyncio.new_event_loop())


def _prepare_connection(monkeypatch: pytest.MonkeyPatch, interval: int = 45, count: int = 6) -> Connection:
    # Native mode composes app-level SSH options from ``ssh_overrides`` (the flat
    # list produced by Preferences ▸ SSH Settings) and appends them verbatim to
    # the command, so keepalive is provided that way rather than via the old
    # ``keepalive_interval`` / ``keepalive_count_max`` keys.
    class _ConfigStub:
        def get_ssh_config(self) -> dict:
            return {
                'ssh_overrides': [
                    '-o', f'ServerAliveInterval={interval}',
                    '-o', f'ServerAliveCountMax={count}',
                ],
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


# Note: per-connection port forwarding is now expressed as LocalForward/
# RemoteForward/DynamicForward directives in ~/.ssh/config (handled by ssh
# itself in the single native command), not by spawning a separate ssh process
# per rule. The former ``Connection.start_local_forwarding`` /
# ``start_remote_forwarding`` subprocess methods were removed; config-based
# forwarding output is covered by tests/test_connection_forwarding.py.
