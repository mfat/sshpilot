"""Tests for connection display helpers."""

from types import SimpleNamespace

from sshpilot.connection_display import (
    format_connection_host_display,
    format_connection_row_tooltip_markup,
)


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


def test_row_tooltip_markup_bold_title_and_dim_host():
    connection = make_connection(
        nickname="prod",
        display_name="prod",
        hostname="prod.example.com",
        username="alice",
        port=2222,
    )

    markup = format_connection_row_tooltip_markup(connection)

    assert markup.startswith("<b>prod</b>")
    assert "alice@prod.example.com:2222" in markup
    assert "alpha='70%'" in markup


def test_row_tooltip_markup_hides_host_and_proxy_when_privacy_on():
    connection = make_connection(
        nickname="prod",
        display_name="prod",
        hostname="prod.example.com",
        username="alice",
        proxy_jump=("bastion",),
        tags=("web",),
    )

    markup = format_connection_row_tooltip_markup(connection, hide_hosts=True)

    assert "<b>prod</b>" in markup
    assert "prod.example.com" not in markup
    assert "bastion" not in markup
    assert "web" in markup


def test_row_tooltip_markup_escapes_special_characters():
    connection = make_connection(
        nickname="a<b>&c",
        display_name="a<b>&c",
        hostname="h.example.com",
        username="u",
    )

    markup = format_connection_row_tooltip_markup(connection)

    assert "<b>a&lt;b&gt;&amp;c</b>" in markup
    assert "a<b>&c" not in markup


def test_row_tooltip_markup_includes_tags_proxy_and_forwarding():
    connection = make_connection(
        nickname="prod",
        display_name="prod",
        hostname="prod.example.com",
        username="alice",
        tags=["production", "web"],
        proxy_jump=["bastion1", "bastion2"],
        forwarding_rules=[
            {
                "type": "local",
                "listen_port": 8080,
                "remote_host": "localhost",
                "remote_port": 80,
                "enabled": True,
            },
        ],
    )

    markup = format_connection_row_tooltip_markup(connection)

    assert "production, web" in markup
    assert "bastion1, bastion2" in markup
    assert "<b>Forwards:</b>" in markup
    assert "8080" in markup
