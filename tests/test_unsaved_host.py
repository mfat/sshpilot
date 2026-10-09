"""Tests for frontend-only save-prompt dismissal state.

Destination matching itself is tested through the daemon application service;
the GTK helper must not inspect SSH config or a connection manager.
"""

from types import SimpleNamespace

from sshpilot.unsaved_host import (
    SavePromptDismissals,
    connection_destination,
    connection_target,
    identity_key,
)


def _conn(**kwargs):
    return SimpleNamespace(
        hostname=kwargs.get("hostname", ""),
        host=kwargs.get("host", ""),
        nickname=kwargs.get("nickname", ""),
        username=kwargs.get("username", ""),
        protocol=kwargs.get("protocol", "ssh"),
        data=kwargs.get("data", {}),
    )


def test_identity_key_format():
    assert identity_key("Host", "User") == "host|User"


def test_destination_uses_projection_fields_without_resolution():
    assert connection_destination(_conn(hostname="Raw.Host", username="sam")) == (
        "raw.host",
        "sam",
    )
    assert connection_destination(_conn(host="alias", nickname="Alias")) == (
        "alias",
        "",
    )


def test_dismissals_are_session_scoped():
    dismissals = SavePromptDismissals()
    connection = _conn(hostname="host", username="user")
    assert not dismissals.is_connection_dismissed(connection)
    dismissals.dismiss_connection(connection)
    assert dismissals.is_connection_dismissed(connection)


def test_dismissing_one_protocol_leaves_the_others_prompting():
    dismissals = SavePromptDismissals()
    dismissals.dismiss_connection(_conn(hostname="localhost", username="me"))
    assert dismissals.is_connection_dismissed(_conn(hostname="localhost", username="me"))
    assert not dismissals.is_connection_dismissed(
        _conn(hostname="localhost", username="me", protocol="mosh")
    )


def test_target_names_a_hostless_protocol_by_its_required_fields():
    serial = _conn(protocol="serial", nickname="dev-ttyUSB0",
                   data={"device": "/dev/ttyUSB0", "baud": "9600"})
    assert connection_target(serial, ["device"]) == (("device", "/dev/ttyUSB0"),)
    # Host protocols are matched on host, user and port by the daemon.
    mosh = _conn(protocol="mosh", hostname="web", data={"host": "web"})
    assert connection_target(mosh, ["host"]) == ()
    assert connection_target(mosh, []) == ()
