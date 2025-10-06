from types import SimpleNamespace

from sshpilot import window


class _DummyConfig:
    def __init__(self, *_, **__):
        pass

    def get_ssh_config(self):
        return {}


class _DummyConnectionManager:
    def get_password(self, *_):
        return None

    def get_key_passphrase(self, *_):
        return None

    def prepare_key_for_connection(self, *_):
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
