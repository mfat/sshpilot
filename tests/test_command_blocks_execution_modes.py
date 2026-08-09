"""Explicit Command Blocks one-shot versus interactive-terminal routing."""

from unittest.mock import Mock

from sshpilot.command_blocks import (
    CommandBlocksPanel,
    DEFAULT_COMMANDS,
    EXECUTION_MODE_INTERACTIVE_TERMINAL,
    EXECUTION_MODE_ONE_SHOT,
    _execution_mode,
)


def test_streaming_and_pty_defaults_are_explicitly_interactive():
    by_id = {command["id"]: command for command in DEFAULT_COMMANDS}
    for command_id in ("c-dlogs", "c-dexec", "c-syslog", "c-journal"):
        assert _execution_mode(by_id[command_id]) == EXECUTION_MODE_INTERACTIVE_TERMINAL
    assert _execution_mode(by_id["c-dps"]) == EXECUTION_MODE_ONE_SHOT


def test_legacy_seeded_interactive_ids_remain_interactive_without_text_parsing():
    assert _execution_mode({"id": "c-dexec", "command": "changed by user"}) == (
        EXECUTION_MODE_INTERACTIVE_TERMINAL
    )
    assert _execution_mode({"id": "custom", "command": "tail -f anything"}) == (
        EXECUTION_MODE_ONE_SHOT
    )


def test_one_shot_multiselect_uses_one_daemon_submission():
    panel = object.__new__(CommandBlocksPanel)
    panel._submit_connections = Mock()
    panel._run_interactive_connections = Mock()
    connections = [Mock(), Mock()]
    panel._dispatch_to_target(
        "uptime", execution_mode=EXECUTION_MODE_ONE_SHOT, connections=connections
    )
    panel._submit_connections.assert_called_once_with(connections, "uptime", None)
    panel._run_interactive_connections.assert_not_called()


def test_interactive_multiselect_uses_terminal_session_path_only():
    panel = object.__new__(CommandBlocksPanel)
    panel._submit_connections = Mock()
    panel._run_interactive_connections = Mock()
    connections = [Mock(), Mock()]
    panel._dispatch_to_target(
        "journalctl -f",
        execution_mode=EXECUTION_MODE_INTERACTIVE_TERMINAL,
        connections=connections,
    )
    panel._run_interactive_connections.assert_called_once_with(
        connections, "journalctl -f", None
    )
    panel._submit_connections.assert_not_called()


def test_interactive_command_is_rejected_by_headless_broadcast_action():
    panel = object.__new__(CommandBlocksPanel)
    panel._show_toast = Mock()
    panel._do_broadcast = Mock()
    panel._broadcast_command(
        {"id": "custom", "execution_mode": EXECUTION_MODE_INTERACTIVE_TERMINAL}
    )
    panel._show_toast.assert_called_once()
    panel._do_broadcast.assert_not_called()
