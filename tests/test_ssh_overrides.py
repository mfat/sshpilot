import asyncio

from sshpilot.connection_manager import Connection
from sshpilot.preferences import PreferencesWindow


class DummyConfig:
    def __init__(self):
        self.settings = {}
        self.default_config = {
            'ssh': {
                'connection_timeout': 30,
                'connection_attempts': 1,
                'keepalive_interval': 60,
                'keepalive_count_max': 3,
                'auto_add_host_keys': True,
                'batch_mode': False,
                'compression': False,
                'verbosity': 0,
                'debug_enabled': False,
                'ssh_overrides': [],
            },
            'file_manager': {
                'force_internal': False,
                'open_externally': False,
            },
        }

    def set_setting(self, key, value):
        self.settings[key] = value

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def get_default_config(self):
        return {
            'ssh': dict(self.default_config['ssh']),
            'file_manager': dict(self.default_config['file_manager']),
        }


class DummySwitchRow:
    def __init__(self, active=False):
        self._active = bool(active)

    def get_active(self):
        return self._active

    def set_active(self, value):
        self._active = bool(value)


class DummySpinRow:
    def __init__(self, value=0):
        self._value = value

    def get_value(self):
        return self._value

    def set_value(self, value):
        self._value = value


class DummyComboRow:
    def __init__(self, selected=0):
        self._selected = selected

    def get_selected(self):
        return self._selected

    def set_selected(self, value):
        self._selected = value


def _build_preferences(**values):
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.config = values.get('config', DummyConfig())
    prefs.parent_window = values.get('parent_window', None)
    prefs.native_connect_row = values.get('native_connect_row', DummySwitchRow(True))
    prefs.connect_timeout_row = values.get('connect_timeout_row', DummySpinRow(12))
    prefs.connection_attempts_row = values.get('connection_attempts_row', DummySpinRow(4))
    prefs.keepalive_interval_row = values.get('keepalive_interval_row', DummySpinRow(45))
    prefs.keepalive_count_row = values.get('keepalive_count_row', DummySpinRow(6))
    prefs.strict_host_row = values.get('strict_host_row', DummyComboRow(1))
    prefs.batch_mode_row = values.get('batch_mode_row', DummySwitchRow(False))
    prefs.compression_row = values.get('compression_row', DummySwitchRow(True))
    prefs.verbosity_row = values.get('verbosity_row', DummySpinRow(2))
    prefs.debug_enabled_row = values.get('debug_enabled_row', DummySwitchRow(True))
    prefs.force_internal_file_manager_row = values.get('force_internal_file_manager_row', None)
    prefs.open_file_manager_externally_row = values.get('open_file_manager_externally_row', None)
    return prefs


def test_save_advanced_ssh_settings_persists_overrides():
    prefs = _build_preferences(batch_mode_row=DummySwitchRow(True))
    prefs.save_advanced_ssh_settings()

    overrides = prefs.config.settings.get('ssh.ssh_overrides')
    assert overrides == [
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=12',
        '-o', 'ConnectionAttempts=4',
        '-o', 'ServerAliveInterval=45',
        '-o', 'ServerAliveCountMax=6',
        '-o', 'StrictHostKeyChecking=yes',
        '-C',
        '-v', '-v',
        '-o', 'LogLevel=DEBUG2',
    ]


def test_apply_default_clears_overrides():
    config = DummyConfig()
    config.set_setting('ssh.ssh_overrides', ['-o', 'Something'])
    prefs = _build_preferences(config=config)

    prefs._apply_default_advanced_settings()

    assert prefs.config.settings['ssh.ssh_overrides'] == [
        '-o', 'ConnectTimeout=30',
        '-o', 'ConnectionAttempts=1',
        '-o', 'ServerAliveInterval=60',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=accept-new',
    ]


def test_native_connect_appends_overrides_even_when_native_disabled(monkeypatch):
    overrides = ['-o', 'ConnectTimeout=10', '-C']

    class NativeConfig:
        def get_ssh_config(self):
            return {
                'native_connect': False,
                'ssh_overrides': overrides,
            }

    monkeypatch.setattr('sshpilot.config.Config', lambda: NativeConfig())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        connection = Connection({'host': 'example.com', 'nickname': 'example'})
        result = loop.run_until_complete(connection.native_connect())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert result is True
    assert connection.ssh_cmd == ['ssh', '-o', 'ConnectTimeout=10', '-C', 'example.com']


def test_dynamic_forwarding_uses_configured_keepalive(monkeypatch):
    executed_commands = []

    async def fake_exec(*cmd, **kwargs):
        executed_commands.append(list(cmd))

        class DummyProcess:
            def __init__(self):
                self.stdout = kwargs.get('stdout')
                self.stderr = kwargs.get('stderr')
                self.returncode = 0

            async def communicate(self):
                return b'', b''

            def terminate(self):
                return None

            async def wait(self):
                return self.returncode

        return DummyProcess()

    class ForwardConfig:
        def get_ssh_config(self):
            return {
                'connection_timeout': 15,
                'connection_attempts': 2,
                'keepalive_interval': 42,
                'keepalive_count_max': 7,
                'batch_mode': True,
                'strict_host_key_checking': 'yes',
            }

    monkeypatch.setattr('sshpilot.config.Config', lambda: ForwardConfig())
    monkeypatch.setattr('sshpilot.connection_manager.asyncio.create_subprocess_exec', fake_exec)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        connection = Connection({'host': 'example.com', 'username': 'user'})
        loop.run_until_complete(connection.start_dynamic_forwarding('127.0.0.1', 9000))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert executed_commands, 'Dynamic forwarding should invoke ssh'
    ssh_cmd = executed_commands[0]

    assert ssh_cmd.count('BatchMode=yes') == 1
    assert ssh_cmd.count('ServerAliveInterval=42') == 1
    assert ssh_cmd.count('ServerAliveCountMax=7') == 1
    assert 'ServerAliveInterval=30' not in ssh_cmd
    assert 'ServerAliveCountMax=3' not in ssh_cmd
