import sys
import types
from types import SimpleNamespace

if 'cairo' not in sys.modules:
    sys.modules['cairo'] = types.ModuleType('cairo')

from sshpilot import window
from sshpilot import scp_window


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
    """Stands in for MainWindow as the ScpWindowController's collaborator."""

    def __init__(self):
        self.connection_manager = _DummyConnectionManager()
        self.config = _DummyConfig()


def _make_ctrl(dummy_window):
    ctrl = scp_window.ScpWindowController.__new__(scp_window.ScpWindowController)
    ctrl.window = dummy_window
    ctrl._scp_auth = None
    ctrl._scp_askpass_env = {}
    ctrl._scp_strip_askpass = False
    ctrl._scp_askpass_helpers = []
    return ctrl


def _scp_argv(dummy_window, *args, **kwargs):
    return _make_ctrl(dummy_window)._build_scp_argv(*args, **kwargs)


def test_build_scp_argv_skips_key_prep_when_identity_agent_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(window, 'Config', _DummyConfig)
    monkeypatch.setattr(scp_window, 'Config', _DummyConfig)

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

    _scp_argv(
        dummy_window,
        connection,
        ['local.txt'],
        '/remote/path',
        direction='upload',
    )

    assert dummy_window.connection_manager.prepare_calls == []


def test_build_scp_argv_prefers_alias_and_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(window, 'Config', _DummyConfig)
    monkeypatch.setattr(scp_window, 'Config', _DummyConfig)

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

    argv = _scp_argv(
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


def test_build_scp_argv_adds_recursive_for_directories(monkeypatch, tmp_path):
    monkeypatch.setattr(window, 'Config', _DummyConfig)
    monkeypatch.setattr(scp_window, 'Config', _DummyConfig)

    key_path = tmp_path / 'id_test_key'
    key_path.write_text('dummy')

    source_dir = tmp_path / 'folder'
    source_dir.mkdir()

    connection = SimpleNamespace(
        hostname='example.com',
        username='alice',
        keyfile=str(key_path),
        key_select_mode=1,
        auth_method=2,
        port=22,
    )

    dummy_window = _DummyWindow()

    argv = _scp_argv(
        dummy_window,
        connection,
        [str(source_dir)],
        '/remote/path',
        direction='upload',
    )

    assert '-r' in argv
    assert any(arg.endswith('/remote/path') for arg in argv)


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

    # An explicit key that exists on disk: the SCP builder only pins ``-i`` +
    # ``IdentitiesOnly`` when the keyfile is a real file (see
    # scp_utils._build_scp_argv_prefix). key_mode == 1 means "use only this key".
    keyfile = tmp_path / 'id_test'
    keyfile.write_text('key')

    base_env = {'BASE': '1'}

    local_dir = tmp_path / 'downloads'
    result = window.download_file(
        'example.com',
        'alice',
        '/remote/file.txt',
        str(local_dir),
        port=2200,
        known_hosts_path='/tmp/known_hosts',
        extra_ssh_opts=['-i', str(keyfile)],
        inherit_env=base_env,
        saved_passphrase='secret',
        keyfile=str(keyfile),
        key_mode=1,
    )

    argv = recorded['argv']
    assert result is True
    assert argv[0] == 'scp'
    assert '-P' in argv and '2200' in argv
    # The native SCP command is built from _build_base_ssh_command (not the old
    # get_scp_ssh_options merge); key_mode == 1 pins the explicit identity.
    assert 'IdentitiesOnly=yes' in argv
    assert '-i' in argv and str(keyfile) in argv
    assert 'UserKnownHostsFile=/tmp/known_hosts' in argv
    # Remote source and local destination round out the transfer.
    assert 'alice@example.com:/remote/file.txt' in argv
    assert str(local_dir) in argv
    # The caller's environment is copied and preserved.
    assert recorded['env']['BASE'] == '1'


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
