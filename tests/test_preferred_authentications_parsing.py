from sshpilot.connection_manager import ConnectionManager


def test_preferred_authentications_parses_order_and_auth_method():
    cm = ConnectionManager.__new__(ConnectionManager)
    config = {
        'host': 'example',
        'preferredauthentications': 'gssapi-with-mic,hostbased,publickey,keyboard-interactive,password',
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    assert parsed['preferred_authentications'] == [
        'gssapi-with-mic',
        'hostbased',
        'publickey',
        'keyboard-interactive',
        'password',
    ]
    assert parsed['auth_method'] == 0
