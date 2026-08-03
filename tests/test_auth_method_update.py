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


def test_key_auth_does_not_preserve_password_preference():
    cm = make_cm()
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'preferred_authentications': ['keyboard-interactive', 'password'],
    }
    entry = ConnectionManager.format_ssh_config_entry(cm, data)
    assert 'PreferredAuthentications' not in entry
    assert 'PubkeyAuthentication no' not in entry


def test_key_auth_keeps_explicit_non_password_preference():
    cm = make_cm()
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'preferred_authentications': ['publickey', 'hostbased'],
    }
    entry = ConnectionManager.format_ssh_config_entry(cm, data)
    assert 'PreferredAuthentications publickey,hostbased' in entry


def test_key_auth_keeps_password_fallback_when_password_present():
    cm = make_cm()
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'password': 'secret',
        'preferred_authentications': ['publickey', 'keyboard-interactive', 'password'],
    }
    entry = ConnectionManager.format_ssh_config_entry(cm, data)
    assert 'PreferredAuthentications publickey,keyboard-interactive,password' in entry
