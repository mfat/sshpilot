"""Explicit Command Blocks one-shot versus interactive-terminal routing."""

from unittest.mock import Mock, patch

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


def test_legacy_interactive_default_without_mode_stays_interactive():
    assert _execution_mode({"id": "c-dexec"}) == (
        EXECUTION_MODE_INTERACTIVE_TERMINAL
    )


def test_explicit_one_shot_overrides_legacy_interactive_default():
    assert _execution_mode(
        {"id": "c-dexec", "execution_mode": EXECUTION_MODE_ONE_SHOT}
    ) == EXECUTION_MODE_ONE_SHOT


def test_command_text_does_not_infer_interactive_execution():
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


def test_interactive_target_insertion_preserves_insert_only():
    terminal = Mock(is_connected=True)
    CommandBlocksPanel._feed_interactive_when_connected(
        terminal, "vim file", insert_only=True
    )
    terminal.feed_child_data.assert_called_once_with(b"vim file")


def test_interactive_target_execution_appends_newline_when_not_insert_only():
    terminal = Mock(is_connected=True)
    CommandBlocksPanel._feed_interactive_when_connected(
        terminal, "tail -f file", insert_only=False
    )
    terminal.feed_child_data.assert_called_once_with(b"tail -f file\n")


def test_delayed_interactive_target_preserves_insert_only_after_connection():
    terminal = Mock(is_connected=False)
    CommandBlocksPanel._feed_interactive_when_connected(
        terminal, "vim file", insert_only=True
    )
    connected = terminal.connect.call_args.args[1]
    with patch("sshpilot.command_blocks.GObject.signal_handler_disconnect"):
        connected(terminal)
    terminal.feed_child_data.assert_called_once_with(b"vim file")


def test_custom_command_dialog_exposes_explicit_interactive_choice():
    from sshpilot import command_blocks

    panel = object.__new__(CommandBlocksPanel)
    panel.window = Mock()
    panel._dispatch_to_target = Mock()
    dialog = Mock()
    entry = Mock()
    entry.get_text.return_value = "top"
    interactive = Mock()
    interactive.get_active.return_value = True

    with (
        patch.object(command_blocks.Adw, "AlertDialog", return_value=dialog),
        patch.object(command_blocks.Adw, "SwitchRow", return_value=interactive),
        patch.object(command_blocks.Gtk, "Entry", return_value=entry),
        patch.object(command_blocks.Gtk, "Box", return_value=Mock()),
    ):
        panel._show_custom_command_dialog(connection="host")

    response = next(call.args[1] for call in dialog.connect.call_args_list if call.args[0] == "response")
    response(dialog, "run")
    panel._dispatch_to_target.assert_called_once_with(
        "top",
        None,
        execution_mode=EXECUTION_MODE_INTERACTIVE_TERMINAL,
        connection="host",
        group=None,
        connections=None,
    )
