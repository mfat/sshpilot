"""Typed routing tests for Run Command on saved host selections."""

import pytest

try:
    import sshpilot.command_blocks as command_blocks
except Exception:  # pragma: no cover - depends on GTK test stubs
    command_blocks = None

pytestmark = pytest.mark.skipif(command_blocks is None, reason="GTK stubs unavailable")


def _panel_with_recorders(group_connections=None):
    panel = command_blocks.CommandBlocksPanel.__new__(command_blocks.CommandBlocksPanel)
    calls = []
    panel._run_interactive_connections = lambda conns, text, cmd_id=None: calls.append(
        ("interactive", list(conns), text, cmd_id)
    )
    panel._group_connections = lambda _group: list(group_connections or [])
    return panel, calls


def test_multiple_connections_open_terminal_session():
    panel, calls = _panel_with_recorders()
    c1, c2 = object(), object()
    panel._dispatch_to_target("uptime", "cmd1", connections=[c1, c2])
    assert calls == [("interactive", [c1, c2], "uptime", "cmd1")]


def test_single_connection_uses_terminal_session_service():
    panel, calls = _panel_with_recorders()
    connection = object()
    panel._dispatch_to_target("uptime", "cmd1", connection=connection)
    assert calls == [("interactive", [connection], "uptime", "cmd1")]


def test_group_resolves_once_then_opens_terminal_session():
    c1, c2 = object(), object()
    panel, calls = _panel_with_recorders([c1, c2])
    panel._dispatch_to_target("uptime", None, group={"name": "prod"})
    assert calls == [("interactive", [c1, c2], "uptime", None)]


def test_empty_connections_falls_through_to_connection():
    panel, calls = _panel_with_recorders()
    connection = object()
    panel._dispatch_to_target("uptime", None, connections=[], connection=connection)
    assert calls == [("interactive", [connection], "uptime", None)]

def test_group_members_without_remote_command_are_left_out(monkeypatch):
    from types import SimpleNamespace

    from sshpilot.plugins import registry
    from sshpilot.plugins.api import Capability

    monkeypatch.setattr(
        registry,
        "capabilities_for",
        lambda c: frozenset({Capability.REMOTE_COMMAND}) if c.protocol == "ssh" else frozenset(),
    )
    web = SimpleNamespace(nickname="web", protocol="ssh")
    console = SimpleNamespace(nickname="console", protocol="serial")
    other = SimpleNamespace(nickname="other", protocol="ssh")
    panel = command_blocks.CommandBlocksPanel.__new__(command_blocks.CommandBlocksPanel)
    panel.window = SimpleNamespace(connection_manager=SimpleNamespace(connections=[web, console, other]))
    toasts = []
    panel._show_toast = toasts.append

    assert panel._group_connections({"connections": ["web", "console"]}) == [web]
    assert toasts == []

    assert panel._group_connections({"connections": ["console"]}) == []
    assert toasts == ["No connections in group can run commands"]
