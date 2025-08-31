import types
from sshpilot import ssh_utils


def test_key_auth_with_optional_password_adds_combined_options():
    conn = types.SimpleNamespace(auth_method=0, password='secret', key_select_mode=0)
    opts = ssh_utils.build_connection_ssh_options(conn)
    assert (
        'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
        in opts
    )
    assert 'PubkeyAuthentication=no' not in opts


def test_key_auth_without_password_omits_combined_options():
    conn = types.SimpleNamespace(auth_method=0, key_select_mode=0)
    opts = ssh_utils.build_connection_ssh_options(conn)
    assert (
        'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
        not in opts
    )
