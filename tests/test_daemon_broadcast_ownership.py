"""GTK broadcast target selection routes one typed request to the daemon."""

from unittest.mock import Mock, patch

from sshpilot.api.models.broadcast import BroadcastCommandRequest
from sshpilot.terminal_manager import TerminalManager


def _terminal(name):
    terminal = Mock()
    terminal.connection.nickname = name
    terminal.feed_child_data = Mock()
    terminal._daemon_controller = Mock()
    return terminal


def _manager(terminals):
    window = Mock()
    window.client = Mock()
    window.client_bridge = Mock()
    manager = TerminalManager(window)
    return window, manager, patch.object(manager, "iter_ssh_terminals", return_value=terminals)


def test_collects_unique_saved_connection_ids_in_display_order():
    window, manager, terminals = _manager([_terminal("one"), _terminal("two"), _terminal("one")])
    with terminals:
        assert manager.broadcast_connection_ids() == ("one", "two")


def test_submits_one_typed_daemon_request_without_terminal_injection():
    first, second = _terminal("one"), _terminal("two")
    window, manager, terminals = _manager([first, second])
    with terminals:
        manager.broadcast_command("uptime")
    operation = window.client_bridge.submit.call_args.args[0]
    operation()
    request = window.client.start_broadcast_command.call_args.args[0]
    assert isinstance(request, BroadcastCommandRequest)
    assert request.connection_ids == ("one", "two")
    assert request.command == "uptime"
    first.feed_child_data.assert_not_called()
    second._daemon_controller.send_input.assert_not_called()


def test_empty_target_set_does_not_submit():
    window, manager, terminals = _manager([])
    with terminals:
        assert manager.broadcast_command("uptime") is None
    window.client_bridge.submit.assert_not_called()


def test_empty_command_does_not_submit():
    window, manager, terminals = _manager([_terminal("one")])
    with terminals:
        assert manager.broadcast_command("  ") is None
    window.client_bridge.submit.assert_not_called()
