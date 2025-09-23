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

    dummy_window = SimpleNamespace(connection_manager=_DummyConnectionManager())

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
