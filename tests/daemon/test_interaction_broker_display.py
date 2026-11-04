"""Issue #1365: the askpass DISPLAY placeholder is also ssh's X11 target.

With X11 forwarding on, OpenSSH connects every forwarded window to the
DISPLAY it was started with. The broker must take the placeholder from the
platform (a TCP display on Windows) and never replace a DISPLAY the user set.
"""

from __future__ import annotations

import os

from sshpilot.api.models import ConnectionId, SessionId
from sshpilot.daemon import interaction_broker
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.session_runtime import SessionLaunchSpec


def _environment(monkeypatch, inherited):
    monkeypatch.setattr(
        interaction_broker, "fallback_display", lambda posix: f"fallback{posix}"
    )
    broker = InteractionBroker(secret_timeout=2, host_key_timeout=2)
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    try:
        _argv, environment = broker.prepare_launch(
            SessionLaunchSpec(
                session_id=SessionId("display"),
                connection_id=ConnectionId("conn-display"),
                protocol="ssh",
                hostname="example.test",
                username="alice",
                port=22,
            ),
            lambda _connection_id, **_kwargs: (
                ("/usr/bin/ssh", "-Y", "example"),
                {"PATH": os.environ.get("PATH", ""), **inherited},
            ),
        )
        _argv, operation_env = broker.prepare_operation_launch(
            ("/usr/bin/ssh", "-Y", "alice@example.test"),
            dict(inherited),
            scope_id=SessionId("display-op"),
            connection_id=ConnectionId("conn-display"),
            hostname="example.test",
            username="alice",
        )
    finally:
        broker.close()
    return environment["DISPLAY"], operation_env["DISPLAY"]


def test_unset_display_uses_the_platform_fallback(monkeypatch):
    assert _environment(monkeypatch, {}) == (
        "fallback:sshpilot-daemon",
        "fallback:sshpilot-daemon",
    )


def test_user_display_is_kept(monkeypatch):
    assert _environment(monkeypatch, {"DISPLAY": "localhost:1.0"}) == (
        "localhost:1.0",
        "localhost:1.0",
    )
