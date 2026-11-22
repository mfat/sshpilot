import pytest

from sshpilot.core.connections.ssh_config_loader import _resolve_auth_method_from_config
from sshpilot.ssh_config_formatter import (
    PASSWORD_MODE_PREFERRED_AUTH,
    format_ssh_config_entry,
)


def _reloaded_auth_method(entry):
    config = {}
    for line in entry.splitlines()[1:]:
        key, _, value = line.strip().partition(' ')
        config[key.lower()] = value
    return _resolve_auth_method_from_config(config)[0]


def test_strip_password_directives_when_key_auth():
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'extra_ssh_config': 'PreferredAuthentications password\nPubkeyAuthentication no\nCompression yes',
    }
    entry = format_ssh_config_entry(data)
    assert 'PreferredAuthentications password' not in entry
    assert 'PubkeyAuthentication no' not in entry
    assert 'Compression yes' in entry


def test_key_auth_does_not_preserve_password_preference():
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'preferred_authentications': ['keyboard-interactive', 'password'],
    }
    entry = format_ssh_config_entry(data)
    assert 'PreferredAuthentications' not in entry
    assert 'PubkeyAuthentication no' not in entry


def test_key_auth_keeps_explicit_non_password_preference():
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'preferred_authentications': ['publickey', 'hostbased'],
    }
    entry = format_ssh_config_entry(data)
    assert 'PreferredAuthentications publickey,hostbased' in entry


def test_key_auth_keeps_password_fallback_when_password_present():
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 0,
        'password': 'secret',
        'preferred_authentications': ['publickey', 'keyboard-interactive', 'password'],
    }
    entry = format_ssh_config_entry(data)
    assert 'PreferredAuthentications publickey,keyboard-interactive,password' in entry


def test_password_auth_drops_stale_preference_without_password_methods():
    # Left behind by a key-auth save that stripped the password methods.
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 1,
        'pubkey_auth_no': True,
        'preferred_authentications': ['gssapi-with-mic', 'hostbased', 'publickey'],
    }
    entry = format_ssh_config_entry(data)
    assert f'PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}\n' in entry + '\n'
    assert 'gssapi-with-mic,hostbased,publickey' not in entry
    assert 'PubkeyAuthentication no' in entry


def test_password_auth_keeps_custom_preference_with_password_method():
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 1,
        'preferred_authentications': 'password,keyboard-interactive',
    }
    entry = format_ssh_config_entry(data)
    assert 'PreferredAuthentications password,keyboard-interactive' in entry


@pytest.mark.parametrize('pubkey_auth_no', [True, False])
@pytest.mark.parametrize('preferred', [
    # Left by a key-auth save without a password (login methods stripped).
    ['gssapi-with-mic', 'hostbased', 'publickey'],
    # Left by a key-auth save with a stored password (combined list).
    ['gssapi-with-mic', 'hostbased', 'publickey', 'keyboard-interactive', 'password'],
    ['keyboard-interactive'],
])
def test_switch_to_password_auth_survives_reload(preferred, pubkey_auth_no):
    data = {
        'nickname': 'host1',
        'hostname': 'example.com',
        'username': 'user',
        'auth_method': 1,
        'pubkey_auth_no': pubkey_auth_no,
        'preferred_authentications': preferred,
    }
    entry = format_ssh_config_entry(data)
    assert f'PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}' in entry
    assert _reloaded_auth_method(entry) == 1


# Password mode tries the password first but keeps Kerberos (GSSAPI) and
# host-based login, which OpenSSH tries by default; public keys stay out.
def test_password_mode_list_keeps_gssapi_and_hostbased():
    assert PASSWORD_MODE_PREFERRED_AUTH == (
        "keyboard-interactive,password,gssapi-with-mic,hostbased"
    )
    entry = format_ssh_config_entry({
        'nickname': 'host1', 'hostname': 'example.com', 'auth_method': 1,
    })
    assert f'PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}' in entry
    assert 'PubkeyAuthentication' not in entry
    assert _reloaded_auth_method(entry) == 1


def test_old_password_mode_default_is_upgraded_on_save():
    entry = format_ssh_config_entry({
        'nickname': 'host1', 'hostname': 'example.com', 'auth_method': 1,
        'preferred_authentications': ['keyboard-interactive', 'password'],
    })
    assert f'PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}' in entry


@pytest.mark.parametrize('left_behind', [
    PASSWORD_MODE_PREFERRED_AUTH,
    'keyboard-interactive,password',
])
def test_key_mode_drops_the_password_mode_list(left_behind):
    # Switching back to keys must not keep a list that leaves keys out.
    entry = format_ssh_config_entry({
        'nickname': 'host1', 'hostname': 'example.com', 'auth_method': 0,
        'preferred_authentications': left_behind,
    })
    assert 'PreferredAuthentications' not in entry
    assert _reloaded_auth_method(entry) == 0


def test_existing_password_host_is_upgraded_by_an_unrelated_edit(tmp_path):
    from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    root = tmp_path / "config"
    root.write_text(
        "Host old\n"
        "    HostName 192.0.2.50\n"
        "    PreferredAuthentications keyboard-interactive,password\n"
        "    PubkeyAuthentication no\n",
        encoding="utf-8",
    )
    record = next(
        item for item in load_ssh_configuration(root, isolated=True).connections
        if item.id == "old"
    )
    assert record.data["auth_method"] == 1
    SshConfigStore(root, isolated=True).update(
        "old",
        {**record.data, "nickname": "old", "hostname": "192.0.2.51"},
        expected_generation=0,
    )
    text = root.read_text(encoding="utf-8")
    assert f"    PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}\n" in text
    assert "    PubkeyAuthentication no\n" in text
    assert "    HostName 192.0.2.51\n" in text
    reloaded = next(
        item for item in load_ssh_configuration(root, isolated=True).connections
        if item.id == "old"
    )
    assert reloaded.data["auth_method"] == 1
