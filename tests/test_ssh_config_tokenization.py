from sshpilot.connection_manager import ConnectionManager


def make_cm():
    return ConnectionManager.__new__(ConnectionManager)


def test_parse_host_with_quotes():
    cm = make_cm()
    config = {
        "host": "nick name",
        "hostname": "example.com",
        "user": "user",
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    assert parsed["nickname"] == "nick name"
    assert parsed["host"] == "example.com"
    assert parsed["aliases"] == []


def test_format_host_requotes():
    cm = make_cm()
    data = {
        "nickname": "nick name",
        "host": "example.com",
        "username": "user",
    }
    entry = ConnectionManager.format_ssh_config_entry(cm, data)
    assert entry.splitlines()[0] == 'Host "nick name"'
