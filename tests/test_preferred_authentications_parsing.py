
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration


def _load(tmp_path, text: str):
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return load_ssh_configuration(path, isolated=False)


def test_preferred_authentications_parses_order_and_auth_method(tmp_path):
    result = _load(
        tmp_path,
        "Host example\n"
        "    PreferredAuthentications gssapi-with-mic,hostbased,publickey,keyboard-interactive,password\n",
    )
    record = result.connections[0]
    assert record.data["preferred_authentications"] == [
        'gssapi-with-mic',
        'hostbased',
        'publickey',
        'keyboard-interactive',
        'password',
    ]
    assert record.data["auth_method"] == 0
