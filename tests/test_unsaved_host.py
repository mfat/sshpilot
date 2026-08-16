"""Tests for frontend-only save-prompt dismissal state.

Destination matching itself is tested through the daemon application service;
the GTK helper must not inspect SSH config or a connection manager.
"""

from types import SimpleNamespace

from sshpilot.unsaved_host import (
    SavePromptDismissals,
    connection_destination,
    identity_key,
)


def _conn(**kwargs):
    return SimpleNamespace(
        hostname=kwargs.get("hostname", ""),
        host=kwargs.get("host", ""),
        nickname=kwargs.get("nickname", ""),
        username=kwargs.get("username", ""),
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
