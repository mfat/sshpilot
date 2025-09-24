"""Tests for connection display helpers."""

from types import SimpleNamespace

from sshpilot.connection_display import format_connection_host_display


def make_connection(**kwargs):
    """Helper to construct a simple connection-like object."""

    defaults = {
        "username": "user",
        "hostname": "",
        "host": "",
        "nickname": "",
        "port": 22,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_format_display_uses_nickname_without_alias_suffix():
    connection = make_connection(nickname="Production", host="Production")

    display = format_connection_host_display(connection)

    assert display == "user@Production"


def test_format_display_with_hostname_and_alias():
    connection = make_connection(hostname="example.com", host="prod")

    display = format_connection_host_display(connection)

    assert display == "user@example.com"


def test_format_display_keeps_alias_suffix_when_no_nickname():
    connection = make_connection(host="prod", nickname="")

    display = format_connection_host_display(connection)

    assert display == "user@prod (alias)"
