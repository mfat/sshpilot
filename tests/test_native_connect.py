import asyncio
import logging
import shlex

from sshpilot.connection_manager import Connection
from sshpilot import config as config_module


class DummyConfig:
    def __init__(self):
        self._ssh_config = {
            'apply_advanced': True,
            'batch_mode': True,
            'connection_timeout': 15,
            'connection_attempts': 4,
            'keepalive_interval': 30,
            'keepalive_count_max': 2,
            'strict_host_key_checking': 'no',
            'auto_add_host_keys': False,
            'exit_on_forward_failure': True,
            'compression': False,
        }

    def get_ssh_config(self):
        return self._ssh_config


def run_native_connect(connection: Connection) -> bool:
    loop = asyncio.new_event_loop()
    old_loop = None
    try:
        try:
            old_loop = asyncio.get_event_loop()
        except RuntimeError:
            old_loop = None
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(connection.native_connect())
    finally:
        loop.close()
        asyncio.set_event_loop(old_loop)


def test_native_connect_includes_advanced_options(monkeypatch):
    monkeypatch.setattr(config_module, 'Config', DummyConfig)

    connection = Connection(
        {
            'hostname': 'example.com',
            'nickname': 'example',
            'username': 'alice',
            'extra_ssh_config': 'Compression yes\nUserKnownHostsFile /tmp/custom_known_hosts',
        }
    )

    assert run_native_connect(connection) is True

    ssh_cmd = connection.ssh_cmd
    host_label = connection.resolve_host_identifier()
    assert ssh_cmd[-1] == host_label

    host_index = ssh_cmd.index(host_label)
    advanced_section = ssh_cmd[:host_index]

    def has_option_pair(value: str) -> bool:
        return any(
            advanced_section[idx] == '-o' and advanced_section[idx + 1] == value
            for idx in range(len(advanced_section) - 1)
        )

    assert has_option_pair('BatchMode=yes')
    assert has_option_pair('ConnectTimeout=15')
    assert has_option_pair('ConnectionAttempts=4')
    assert has_option_pair('ServerAliveInterval=30')
    assert has_option_pair('ServerAliveCountMax=2')
    assert has_option_pair('StrictHostKeyChecking=no')
    assert has_option_pair('ExitOnForwardFailure=yes')
    assert has_option_pair('Compression=yes')
    assert has_option_pair('UserKnownHostsFile=/tmp/custom_known_hosts')

    assert has_option('ConnectTimeout=15')
    assert has_option('ConnectionAttempts=4')
    assert has_option('ServerAliveInterval=30')
    assert has_option('ServerAliveCountMax=2')
    assert has_option('StrictHostKeyChecking=no')
    assert has_option('ExitOnForwardFailure=yes')
    assert has_option('Compression=yes')
    assert has_option('UserKnownHostsFile=/tmp/custom_known_hosts')


def test_native_connect_logs_raw_command(monkeypatch, caplog):
    monkeypatch.setattr(config_module, 'Config', DummyConfig)

    connection = Connection(
        {
            'hostname': 'example.com',
            'nickname': 'example',
            'username': 'alice',
            'extra_ssh_config': 'Compression yes\nUserKnownHostsFile /tmp/custom_known_hosts',
        }
    )

    caplog.set_level(logging.INFO, logger='sshpilot.connection_manager')

    assert run_native_connect(connection) is True

    if hasattr(shlex, 'join'):
        expected_command = shlex.join(connection.ssh_cmd)
    else:
        expected_command = ' '.join(connection.ssh_cmd)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == 'sshpilot.connection_manager' and record.levelno == logging.INFO
    ]

    assert any(expected_command in message for message in messages)

