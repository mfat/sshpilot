"""GTK broadcast target selection routes one typed request to the daemon."""

from unittest.mock import Mock, patch

from sshpilot.api.models.terminal import BroadcastTerminalInputRequest
from sshpilot.terminal_manager import TerminalManager


def _terminal(name, session_id=None):
    terminal = Mock()
    terminal.connection.nickname = name
    terminal.is_daemon_backed = True
    terminal.daemon_tab_state = Mock(session_id=session_id or f"session-{name}")
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


def test_submits_one_typed_session_request_without_terminal_injection():
    first, second = _terminal("one", "session-one"), _terminal("two", "session-two")
    window, manager, terminals = _manager([first, second])
    with terminals:
        manager.broadcast_command("uptime")
    operation = window.client_bridge.submit.call_args.args[0]
    operation()
    request = window.client.broadcast_terminal_input.call_args.args[0]
    assert isinstance(request, BroadcastTerminalInputRequest)
    assert request.session_ids == ("session-one", "session-two")
    assert request.command == "uptime"
    window.client.start_broadcast_command.assert_not_called()


def test_session_targets_are_deduplicated():
    window, manager, terminals = _manager(
        [_terminal("one", "session-one"), _terminal("duplicate", "session-one")]
    )
    with terminals:
        assert manager.broadcast_session_ids() == ("session-one",)


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
