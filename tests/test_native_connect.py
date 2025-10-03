import asyncio
import logging
import shlex

from sshpilot.connection_manager import Connection
from sshpilot import config as config_module


class DummyConfig:
    def __init__(self):
        self._settings = {
            'ssh.apply_advanced': True,
            'ssh.batch_mode': True,
            'ssh.connection_timeout': 15,
            'ssh.connection_attempts': 4,
            'ssh.keepalive_interval': 30,
            'ssh.keepalive_count_max': 2,
            'ssh.strict_host_key_checking': 'no',
            'ssh.exit_on_forward_failure': True,
            'ssh.compression': False,
            'ssh.verbosity': 2,
            'ssh.debug_enabled': True,
        }
        self.calls = []

    def get_setting(self, key, default=None):
        self.calls.append(key)
        return self._settings.get(key, default)

    def get_ssh_config(self):  # pragma: no cover - guard against legacy access
        raise AssertionError('native_connect should not call get_ssh_config')


class DisabledAdvancedConfig:
    def __init__(self):
        self._settings = {
            'ssh.apply_advanced': False,
            'ssh.strict_host_key_checking': 'accept-new',
            'ssh.exit_on_forward_failure': True,
        }
        self.calls = []

    def get_setting(self, key, default=None):
        self.calls.append(key)
        return self._settings.get(key, default)

    def get_ssh_config(self):  # pragma: no cover - guard against legacy access
        raise AssertionError('native_connect should not call get_ssh_config')


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
    config_instance = DummyConfig()
    monkeypatch.setattr(config_module, 'Config', lambda: config_instance)

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

    def has_option(value: str) -> bool:
        return has_option_pair(value)

    assert has_option_pair('BatchMode=yes')
    assert has_option_pair('ConnectTimeout=15')
    assert has_option_pair('ConnectionAttempts=4')
    assert has_option_pair('ServerAliveInterval=30')
    assert has_option_pair('ServerAliveCountMax=2')
    assert has_option_pair('StrictHostKeyChecking=no')
    assert has_option_pair('ExitOnForwardFailure=yes')
    assert has_option_pair('Compression=yes')
    assert has_option_pair('UserKnownHostsFile=/tmp/custom_known_hosts')
    assert has_option_pair('LogLevel=DEBUG2')
    assert not has_option_pair('LogLevel=DEBUG')

    assert advanced_section.count('-v') == 2

    assert has_option('ConnectTimeout=15')
    assert has_option('ConnectionAttempts=4')
    assert has_option('ServerAliveInterval=30')
    assert has_option('ServerAliveCountMax=2')
    assert has_option('StrictHostKeyChecking=no')
    assert has_option('ExitOnForwardFailure=yes')
    assert has_option('Compression=yes')
    assert has_option('UserKnownHostsFile=/tmp/custom_known_hosts')

    expected_keys = {
        'ssh.apply_advanced',
        'ssh.batch_mode',
        'ssh.connection_timeout',
        'ssh.connection_attempts',
        'ssh.keepalive_interval',
        'ssh.keepalive_count_max',
        'ssh.strict_host_key_checking',
        'ssh.compression',
        'ssh.exit_on_forward_failure',
        'ssh.verbosity',
        'ssh.debug_enabled',
    }
    assert expected_keys.issubset(set(config_instance.calls))


def test_native_connect_logs_raw_command(monkeypatch, caplog):
    config_instance = DummyConfig()
    monkeypatch.setattr(config_module, 'Config', lambda: config_instance)

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


def test_native_connect_excludes_strict_host_key_checking_when_advanced_disabled(monkeypatch):
    config_instance = DisabledAdvancedConfig()
    monkeypatch.setattr(config_module, 'Config', lambda: config_instance)

    connection = Connection(
        {
            'hostname': 'example.com',
            'nickname': 'example',
            'username': 'alice',
        }
    )

    assert run_native_connect(connection) is True

    assert all(
        not str(option).startswith('StrictHostKeyChecking=')
        for option in connection.ssh_cmd
        if isinstance(option, str)
    )

