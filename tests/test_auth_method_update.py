from sshpilot.connection_manager import ConnectionManager


def make_cm():
    return ConnectionManager.__new__(ConnectionManager)


def test_strip_password_directives_when_key_auth():
    cm = make_cm()
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'extra_ssh_config': 'PreferredAuthentications password\nPubkeyAuthentication no\nCompression yes',
    }
    entry = ConnectionManager.format_ssh_config_entry(cm, data)
    assert 'PreferredAuthentications password' not in entry
    assert 'PubkeyAuthentication no' not in entry
    assert 'Compression yes' in entry
