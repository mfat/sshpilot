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


def test_multiselect_always_routes_through_terminal_session_route():
    panel = object.__new__(CommandBlocksPanel)
    panel._submit_connections = Mock()
    panel._run_interactive_connections = Mock()
    connections = [Mock(), Mock()]
    panel._dispatch_to_target("uptime", connections=connections)
    panel._run_interactive_connections.assert_called_once_with(
        connections, "uptime", None
    )
    panel._submit_connections.assert_not_called()


def test_single_connection_uses_terminal_session_route():
    panel = object.__new__(CommandBlocksPanel)
    panel._submit_connections = Mock()
    panel._run_interactive_connections = Mock()
    connection = Mock()
    panel._dispatch_to_target("uptime", connection=connection)
    panel._run_interactive_connections.assert_called_once_with(
        [connection], "uptime", None
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


def test_single_connection_reuses_existing_terminal_without_opening_new():
    panel = object.__new__(CommandBlocksPanel)
    panel.store = Mock()
    panel.store._config.get_setting.return_value = False
    existing = Mock(is_connected=True)
    connection = Mock()
    panel.window = Mock(
        active_terminals={connection: existing},
        connection_to_terminals={connection: [existing]},
    )
    panel.window._page_for_child.return_value = Mock()
    manager = Mock()
    panel.window.terminal_manager = manager
    panel._feed_interactive_when_connected = Mock()

    panel._run_interactive_connections([connection], "uptime", "cmd1")

    panel.window.terminal_manager.connect_to_host.assert_not_called()
    panel._feed_interactive_when_connected.assert_called_once_with(
        existing, "uptime", insert_only=False
    )
    panel.window.tab_view.set_selected_page.assert_called_once_with(
        panel.window._page_for_child.return_value
    )
    panel.store.record_use.assert_called_once_with("cmd1")


def test_single_connection_opens_new_terminal_when_none_is_open():
    panel = object.__new__(CommandBlocksPanel)
    panel.store = Mock()
    panel.store._config.get_setting.return_value = False
    connection = Mock()
    new_terminal = Mock(is_connected=True)
    panel.window = Mock(active_terminals={}, connection_to_terminals={})
    manager = Mock()
    panel.window.terminal_manager = manager

    def _connect(conn, force_new):
        panel.window.active_terminals[conn] = new_terminal

    manager.connect_to_host.side_effect = _connect
    panel._feed_interactive_when_connected = Mock()

    panel._run_interactive_connections([connection], "uptime", "cmd1")

    manager.connect_to_host.assert_called_once_with(connection, force_new=True)
    panel._feed_interactive_when_connected.assert_called_once_with(
        new_terminal, "uptime", insert_only=False
    )


def test_custom_command_dialog_routes_through_terminal_session():
    from sshpilot import command_blocks

    panel = object.__new__(CommandBlocksPanel)
    panel.window = Mock()
    panel._dispatch_to_target = Mock()
    dialog = Mock()
    entry = Mock()
    entry.get_text.return_value = "top"

    with (
        patch.object(command_blocks.Adw, "AlertDialog", return_value=dialog),
        patch.object(command_blocks.Gtk, "Entry", return_value=entry),
        patch.object(command_blocks.Gtk, "Box", return_value=Mock()),
    ):
        panel._show_custom_command_dialog(connection="host")

    response = next(call.args[1] for call in dialog.connect.call_args_list if call.args[0] == "response")
    response(dialog, "run")
    panel._dispatch_to_target.assert_called_once_with(
        "top",
        None,
        connection="host",
        group=None,
        connections=None,
    )
