import sys
import types
from types import SimpleNamespace

if 'cairo' not in sys.modules:
    sys.modules['cairo'] = types.ModuleType('cairo')

from sshpilot import askpass_utils, window


class _DummyConfig:
    def __init__(self, *_, **__):
        pass

    def get_ssh_config(self):
        return {}


class _DummyConnectionManager:
    def __init__(self):
        self.prepare_calls = []

    def get_password(self, *_):
        return None

    def get_key_passphrase(self, *_):
        return None

    def prepare_key_for_connection(self, *args):
        self.prepare_calls.append(args[0] if args else None)
        return True


class _DummyWindow:
    def __init__(self):
        self.connection_manager = _DummyConnectionManager()

    def _append_scp_option_pair(self, options, flag, value):
        return window.MainWindow._append_scp_option_pair(self, options, flag, value)

    def _extend_scp_options_from_connection(self, connection, options):
        return window.MainWindow._extend_scp_options_from_connection(self, connection, options)

    def _build_scp_connection_profile(self, connection):
        return window.MainWindow._build_scp_connection_profile(self, connection)


def _build_args(monkeypatch, tmp_path, key_mode):
    monkeypatch.setattr(window, 'Config', _DummyConfig)

    key_path = tmp_path / 'id_test_key'
    key_path.write_text('dummy')

    connection = SimpleNamespace(
        hostname='example.com',
        username='alice',
        keyfile=str(key_path),
        key_select_mode=key_mode,
        auth_method=2,
        port=22,
    )

    dummy_window = _DummyWindow()

    argv = window.MainWindow._build_scp_argv(
        dummy_window,
        connection,
        ['local.txt'],
        '/remote/path',
        direction='upload',
    )

    return argv, str(key_path)


def test_build_scp_argv_mode_1_adds_identity_options(monkeypatch, tmp_path):
    argv, key_path = _build_args(monkeypatch, tmp_path, key_mode=1)

    assert '-i' in argv
    assert argv[argv.index('-i') + 1] == key_path
    assert 'IdentitiesOnly=yes' in argv


def test_build_scp_argv_mode_2_skips_identities_only(monkeypatch, tmp_path):
    argv, key_path = _build_args(monkeypatch, tmp_path, key_mode=2)

    assert '-i' in argv
    assert argv[argv.index('-i') + 1] == key_path
    assert 'IdentitiesOnly=yes' not in argv


def test_build_scp_argv_skips_key_prep_when_identity_agent_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(window, 'Config', _DummyConfig)

    key_path = tmp_path / 'id_test_key'
    key_path.write_text('dummy')

    connection = SimpleNamespace(
        hostname='example.com',
        username='alice',
        keyfile=str(key_path),
        key_select_mode=1,
        auth_method=2,
        port=22,
        identity_agent_disabled=True,
    )

    dummy_window = _DummyWindow()

    window.MainWindow._build_scp_argv(
        dummy_window,
        connection,
        ['local.txt'],
        '/remote/path',
        direction='upload',
    )

    assert dummy_window.connection_manager.prepare_calls == []


def test_build_scp_argv_prefers_alias_and_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(window, 'Config', _DummyConfig)

    config_path = tmp_path / 'ssh_config'
    config_path.write_text('Host testbox\n    HostName example.com\n')

    connection = SimpleNamespace(
        host='testbox',
        hostname='example.com',
        username='alice',
        keyfile='',
        key_select_mode=0,
        auth_method=0,
        port=2224,
        proxy_jump=['bastion.example.org'],
        proxy_command='ssh proxy nc %h %p',
        config_root=str(config_path),
    )

    dummy_window = _DummyWindow()
    dummy_window.connection_manager.ssh_config_path = str(config_path)

    argv = window.MainWindow._build_scp_argv(
        dummy_window,
        connection,
        ['local.txt'],
        '/remote/path',
        direction='upload',
    )

    assert '-F' in argv
    assert argv[argv.index('-F') + 1] == str(config_path)
    assert 'ProxyJump=bastion.example.org' in argv
    assert argv[-1] == 'alice@testbox:/remote/path'


def test_build_scp_argv_skips_key_prep_when_agent_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(window, 'Config', _DummyConfig)

    key_path = tmp_path / 'id_skip'
    key_path.write_text('dummy')

    class RecordingManager(_DummyConnectionManager):
        def __init__(self):
            self.calls = []

        def prepare_key_for_connection(self, key_path):
            self.calls.append(key_path)
            return True

    connection = SimpleNamespace(
        hostname='example.com',
        username='alice',
        keyfile=str(key_path),
        key_select_mode=1,
        auth_method=2,
        port=22,
        identity_agent_disabled=True,
    )

    dummy_window = _DummyWindow()
    dummy_window.connection_manager = RecordingManager()

    window.MainWindow._build_scp_argv(
        dummy_window,
        connection,
        ['local.txt'],
        '/remote/path',
        direction='upload',
    )

    assert dummy_window.connection_manager.calls == []


def test_download_file_with_passphrase_merges_env_and_opts(monkeypatch, tmp_path):
    recorded = {}

    def fake_run(argv, check, text, capture_output, env):
        recorded['argv'] = argv
        recorded['env'] = env

        class _Result:
            returncode = 0
            stderr = ''

        return _Result()

    monkeypatch.setattr(window.subprocess, 'run', fake_run)

    ask_env = {
        'SSH_ASKPASS': '/tmp/fake-askpass',
        'SSH_ASKPASS_REQUIRE': 'force',
        'DISPLAY': ':42',
    }
    monkeypatch.setattr(askpass_utils, 'get_ssh_env_with_forced_askpass', lambda: ask_env)
    monkeypatch.setattr(
        askpass_utils,
        'get_scp_ssh_options',
        lambda: ['-o', 'PreferredAuthentications=publickey', '-o', 'IdentitiesOnly=yes'],
    )

    base_env = {
        'BASE': '1',
        'SSH_ASKPASS': 'old-value',
        'SSH_ASKPASS_REQUIRE': 'old',
    }

    local_dir = tmp_path / 'downloads'
    result = window.download_file(
        'example.com',
        'alice',
        '/remote/file.txt',
        str(local_dir),
        port=2200,
        known_hosts_path='/tmp/known_hosts',
        extra_ssh_opts=['-i', '/tmp/id_test'],
        inherit_env=base_env,
        saved_passphrase='secret',
        keyfile='/tmp/id_test',
        key_mode=1,
    )

    assert result is True
    assert recorded['argv'][0] == 'scp'
    assert '-P' in recorded['argv'] and '2200' in recorded['argv']
    assert 'IdentitiesOnly=yes' in recorded['argv']
    assert 'PreferredAuthentications=publickey' in recorded['argv']
    assert recorded['env']['SSH_ASKPASS'] == '/tmp/fake-askpass'
    assert recorded['env']['SSH_ASKPASS_REQUIRE'] == 'force'
    assert recorded['env']['BASE'] == '1'
    assert base_env['SSH_ASKPASS'] == 'old-value'
    assert base_env['SSH_ASKPASS_REQUIRE'] == 'old'


def test_download_file_without_passphrase_strips_askpass(monkeypatch, tmp_path):
    recorded = {}

    def fake_run(argv, check, text, capture_output, env):
        recorded['env'] = env

        class _Result:
            returncode = 0
            stderr = ''

        return _Result()

    monkeypatch.setattr(window.subprocess, 'run', fake_run)

    base_env = {
        'SSH_ASKPASS': 'something',
        'SSH_ASKPASS_REQUIRE': 'prefer',
    }

    result = window.download_file(
        'example.com',
        'bob',
        '/remote/file.txt',
        str(tmp_path / 'dest'),
        extra_ssh_opts=None,
        inherit_env=base_env,
    )

    assert result is True
    assert 'SSH_ASKPASS' not in recorded['env']
    assert 'SSH_ASKPASS_REQUIRE' not in recorded['env']
    assert base_env['SSH_ASKPASS'] == 'something'
    assert base_env['SSH_ASKPASS_REQUIRE'] == 'prefer'
