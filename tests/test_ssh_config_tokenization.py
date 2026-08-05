import asyncio

from sshpilot.connection_dialog import SSHConnectionValidator
from sshpilot.connection_model import Connection
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.ssh_config_formatter import format_ssh_config_entry

asyncio.set_event_loop(asyncio.new_event_loop())


def _load(tmp_path, text: str):
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return load_ssh_configuration(path, isolated=False)


def test_parse_host_with_quotes(tmp_path):
    result = _load(tmp_path, 'Host "nick name"\n    HostName example.com\n    User user\n')
    record = result.connections[0]
    assert record.nickname == "nick name"
    assert record.hostname == "example.com"
    assert record.data.get("aliases") == []


def test_parse_host_without_hostname_defaults_to_alias(tmp_path):
    result = _load(tmp_path, "Host localhost\n    User mahdi\n")
    record = result.connections[0]
    assert record.data["hostname"] == ""
    assert record.data["host"] == "localhost"
    assert record.nickname == "localhost"


def test_connection_host_preserves_alias_when_hostname_blank():
    data = {
        "host": "alias",
        "hostname": "",
        "username": "user",
    }
    connection = Connection(data)
    assert connection.data["host"] == "alias"
    assert connection.host == "alias"
    assert connection.hostname == ""


def test_connection_update_preserves_alias_when_hostname_blank():
    data = {
        "host": "alias",
        "hostname": "",
        "username": "user",
    }
    connection = Connection(data)
    # Update with new username but keep hostname blank
    connection.update_data({"username": "newuser", "hostname": ""})
    assert connection.host == "alias"
    assert connection.hostname == ""


def test_format_host_requotes():
    data = {
        "nickname": "nick name",
        "hostname": "example.com",
        "username": "user",
    }
    entry = format_ssh_config_entry(data)
    assert entry.splitlines()[0] == 'Host "nick name"'


def test_connection_name_rejects_whitespace():
    validator = SSHConnectionValidator()
    result = validator.validate_connection_name("nick name")
    assert not result.is_valid
    assert "whitespace" in result.message.lower()
