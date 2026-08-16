"""TerminalWidget PTY auto-fill: one-shot typing when a known prompt appears."""
import logging
import types
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget
from sshpilot import command_blocks
from sshpilot.plugins.host import PluginHost


def _term(*, prompt="[sshPilot] sudo password:", response="s3cret"):
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = False
    t._daemon_controller = None
    t._pty_autofill = (prompt, response)
    t._pty_autofill_done = False
    t._pty_autofill_handler = 42
    t._pty_autofill_timeout_id = 99
    fed = []
    t.backend = types.SimpleNamespace(
        supports_feature=lambda feature: feature == "local_process",
        feed_child_data=lambda data: fed.append(data),
    )
    t._scrape_recent_terminal_text = lambda max_chars=2000: (
        f"docker logs\n{prompt}"
    )
    t._fed = fed
    return t


def test_pty_autofill_feeds_response_on_prompt_match():
    t = _term()
    t._on_pty_autofill_changed(None)
    assert t._fed == [b"s3cret\n"]
    assert t._pty_autofill is None
    assert t._pty_autofill_done is True
    assert t._pty_autofill_handler is None
    assert t._pty_autofill_timeout_id is None


def test_pty_autofill_ignores_output_without_prompt():
    t = _term()
    t._scrape_recent_terminal_text = lambda max_chars=2000: "docker logs only"
    t._on_pty_autofill_changed(None)
    assert t._fed == []
    assert t._pty_autofill == ("[sshPilot] sudo password:", "s3cret")
    assert t._pty_autofill_done is False


def test_pty_autofill_does_not_fall_back_to_emulator_internals():
    t = _term()
    t.backend = types.SimpleNamespace(supports_feature=lambda _feature: False)
    t._on_pty_autofill_changed(None)
    assert t._fed == []

def test_cancel_pty_autofill_clears_state():
    t = _term()
    disconnected = []
    t.backend.disconnect = lambda hid: disconnected.append(hid)
    t._cancel_pty_autofill()
    assert t._pty_autofill is None
    assert t._pty_autofill_done is True
    assert disconnected == [42]
    assert t._pty_autofill_handler is None
    assert t._pty_autofill_timeout_id is None


def test_install_pty_autofill_noop_without_config():
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = False
    t._pty_autofill = None
    t._pty_autofills = None
    t._install_pty_autofill()  # must not raise


def test_install_pty_autofill_disabled_for_daemon_mode():
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = True
    t._daemon_controller = object()
    t._pty_autofill = ("Password:", "secret")
    t._pty_autofills = [("Password:", "secret")]
    t._install_pty_autofill()
    assert t._pty_autofill is None
    assert t._pty_autofills is None


def test_daemon_autofill_tick_does_not_feed_local_child():
    t = _term()
    t._daemon_mode = True
    controller = types.SimpleNamespace(sent=[], input_owner=True)
    controller.send_input = lambda data: controller.sent.append(data)
    t._daemon_controller = controller
    t._on_pty_autofill_changed(None)
    assert t._fed == []
    assert controller.sent == []
    assert t._pty_autofill is None


def _password_fill(password="pw123"):
    """The queued ssh-password fill exactly as arm_password_pty_autofill arms it."""
    from sshpilot.askpass_utils import classify_prompt
    return (lambda text: classify_prompt(text) == 'password', password)


def test_queued_password_fill_answers_password_prompt_once():
    t = _term()
    t._pty_autofill = None
    t._pty_autofills = [_password_fill()]
    t._scrape_recent_terminal_text = lambda max_chars=2000: (
        "debug1: Next authentication method: keyboard-interactive\n"
        "(testuser@127.0.0.1) Password:"
    )
    t._on_pty_autofill_changed(None)
    assert t._fed == [b"pw123\n"]
    # One-shot: a re-prompt (rejected password) is left for the user.
    t._on_pty_autofill_changed(None)
    assert t._fed == [b"pw123\n"]


def test_queued_password_fill_ignores_2fa_prompt():
    t = _term()
    t._pty_autofill = None
    t._pty_autofills = [_password_fill()]
    t._scrape_recent_terminal_text = lambda max_chars=2000: (
        "Verification code:"
    )
    t._on_pty_autofill_changed(None)
    assert t._fed == []
    assert t._pty_autofills  # still armed for the real password prompt


def test_queued_password_fill_coexists_with_legacy_sudo_fill():
    t = _term()  # legacy sudo fill armed via _pty_autofill
    t._pty_autofills = [_password_fill()]
    # ssh password prompt first: only the queued fill fires.
    t._scrape_recent_terminal_text = lambda max_chars=2000: "Password:"
    t._on_pty_autofill_changed(None)
    assert t._fed == [b"pw123\n"]
    assert t._pty_autofill is not None
    # then the sudo prompt: the legacy fill fires and everything unwinds.
    t.vte = types.SimpleNamespace(disconnect=lambda hid: None)
    t._scrape_recent_terminal_text = lambda max_chars=2000: (
        "[sshPilot] sudo password:"
    )
    t._on_pty_autofill_changed(None)
    assert t._fed == [b"pw123\n", b"s3cret\n"]
    assert t._pty_autofill is None
    assert t._pty_autofills is None


def test_arm_password_pty_autofill_queues_classifier():
    t = TerminalWidget.__new__(TerminalWidget)
    t._pty_autofills = None
    t.arm_password_pty_autofill("secret")
    assert len(t._pty_autofills) == 1
    matcher, response = t._pty_autofills[0]
    assert response == "secret"
    assert matcher("Password:") is True
    assert matcher("Verification code:") is False


def test_feed_child_data_requires_ownership_for_daemon():
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = True
    shown = []
    t._show_view_only_indicator = lambda: shown.append(True)
    controller = types.SimpleNamespace(sent=[], input_owner=False)
    controller.send_input = lambda data: controller.sent.append(data)
    t._daemon_controller = controller
    t.backend = types.SimpleNamespace(
        feed_child_data=lambda data: (_ for _ in ()).throw(
            AssertionError("local feed must not run")
        )
    )
    t.feed_child_data(b"nope\n")
    assert controller.sent == []
    assert shown == [True]


def test_feed_child_data_daemon_with_ownership_uses_controller():
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = True
    controller = types.SimpleNamespace(sent=[], input_owner=True)
    controller.send_input = lambda data: controller.sent.append(data)
    t._daemon_controller = controller
    t.backend = types.SimpleNamespace(
        feed_child_data=lambda data: (_ for _ in ()).throw(
            AssertionError("local feed must not run")
        )
    )
    t.feed_child_data(b"typed\n")
    assert controller.sent == [b"typed\n"]


def _daemon_terminal(*, subscribed=False, running=False):
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal._daemon_mode = True
    terminal._daemon_controller = Mock(
        input_owner=True,
        session_events_subscribed=subscribed,
        session_running=running,
    )
    terminal.backend = Mock()
    terminal._shell_output_seen = False
    terminal._shell_output_seen_after_running = False
    terminal._pending_shell_ready_feeds = []
    terminal.is_connected = False
    return terminal


def test_daemon_terminal_shell_ready_feed_is_deferred_until_output():
    terminal = _daemon_terminal()
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._daemon_controller.send_input.assert_not_called()
    assert terminal._pending_shell_ready_feeds == [b"curl -s ifconfig.me\n"]


def test_daemon_terminal_shell_ready_feed_flushes_on_first_output_frame():
    terminal = _daemon_terminal()
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._on_daemon_output(b"BusyBox v1.36.1\n")
    terminal._daemon_controller.send_input.assert_called_once_with(
        b"curl -s ifconfig.me\n"
    )
    assert terminal._pending_shell_ready_feeds == []
    assert terminal._shell_output_seen is True


def test_daemon_terminal_shell_ready_feed_after_output_feeds_immediately():
    terminal = _daemon_terminal()
    terminal._on_daemon_output(b"root@host:~# ")
    terminal._daemon_controller.send_input.reset_mock()
    terminal.feed_child_data_when_shell_ready(b"uptime\n")
    terminal._daemon_controller.send_input.assert_called_once_with(b"uptime\n")
    assert terminal._pending_shell_ready_feeds == []


def test_daemon_terminal_shell_ready_running_gate_defers_before_running():
    terminal = _daemon_terminal(subscribed=True)
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._daemon_controller.send_input.assert_not_called()
    assert terminal._pending_shell_ready_feeds == [b"curl -s ifconfig.me\n"]


def test_daemon_terminal_shell_ready_running_gate_defers_after_auth_before_banner():
    terminal = _daemon_terminal(subscribed=True, running=True)
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._daemon_controller.send_input.assert_not_called()
    assert terminal._pending_shell_ready_feeds == [b"curl -s ifconfig.me\n"]


def test_daemon_terminal_shell_ready_running_gate_feeds_when_output_seen_after_running():
    terminal = _daemon_terminal(subscribed=True, running=True)
    terminal._shell_output_seen_after_running = True
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._daemon_controller.send_input.assert_called_once_with(
        b"curl -s ifconfig.me\n"
    )
    assert terminal._pending_shell_ready_feeds == []


def test_daemon_terminal_shell_ready_running_gate_ignores_password_prompt_output():
    terminal = _daemon_terminal(subscribed=True)
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._on_daemon_output(b"user@host's password: ")
    terminal._daemon_controller.send_input.assert_not_called()
    assert terminal._shell_output_seen is True
    assert terminal._shell_output_seen_after_running is False
    assert terminal._pending_shell_ready_feeds == [b"curl -s ifconfig.me\n"]


def test_daemon_terminal_shell_ready_running_gate_flushes_on_output_after_running():
    terminal = _daemon_terminal(subscribed=True)
    terminal.feed_child_data_when_shell_ready(b"curl -s ifconfig.me\n")
    terminal._on_daemon_output(b"user@host's password: ")
    terminal._daemon_controller.send_input.assert_not_called()
    terminal._daemon_controller.session_running = True
    terminal._on_daemon_output(b"BusyBox v1.37.0\nroot@GoogleWifi:~# ")
    terminal._daemon_controller.send_input.assert_called_once_with(
        b"curl -s ifconfig.me\n"
    )
    assert terminal._shell_output_seen_after_running is True
    assert terminal._pending_shell_ready_feeds == []


def test_command_blocks_feed_routes_real_widget_through_shell_ready_gate():
    terminal = _daemon_terminal()
    command_blocks.CommandBlocksPanel._feed_interactive_when_connected(
        terminal, "curl -s ifconfig.me", insert_only=False
    )
    terminal._daemon_controller.send_input.assert_not_called()
    assert terminal._pending_shell_ready_feeds == [b"curl -s ifconfig.me\n"]


def test_command_blocks_feed_real_widget_waits_for_banner_before_send_input():
    terminal = _daemon_terminal()
    command_blocks.CommandBlocksPanel._feed_interactive_when_connected(
        terminal, "curl -s ifconfig.me", insert_only=False
    )
    terminal._on_daemon_output(b"BusyBox v1.36.1\nroot@host:~# ")
    terminal._daemon_controller.send_input.assert_called_once_with(
        b"curl -s ifconfig.me\n"
    )


def test_command_blocks_active_terminal_routes_daemon_input_through_widget():
    terminal = _daemon_terminal()
    panel = command_blocks.CommandBlocksPanel.__new__(command_blocks.CommandBlocksPanel)
    panel.window = Mock()
    panel.window._get_active_terminal_widget.return_value = terminal
    panel.store = Mock()
    panel.store._config.get_setting.return_value = False

    panel._feed_terminal("uptime", "command-id")

    terminal._daemon_controller.send_input.assert_called_once_with(b"uptime\n")
    terminal.backend.feed_child_data.assert_not_called()
    panel.store.record_use.assert_called_once_with("command-id")


def test_command_blocks_specific_terminal_routes_daemon_input_through_widget():
    terminal = _daemon_terminal()
    panel = command_blocks.CommandBlocksPanel.__new__(command_blocks.CommandBlocksPanel)
    panel.window = Mock()
    panel.window._get_active_terminal_widget.return_value = terminal
    panel.store = Mock()
    panel.store._config.get_setting.return_value = False

    panel._feed_terminal("whoami", "command-id")

    terminal._daemon_controller.send_input.assert_called_once_with(b"whoami\n")
    terminal.backend.feed_child_data.assert_not_called()
    panel.store.record_use.assert_called_once_with("command-id")


def test_plugin_send_terminal_does_not_use_widget_as_backend():
    terminal = _daemon_terminal()
    host = PluginHost(connection_manager=None)
    host._terminal_widgets["session-id"] = lambda: terminal

    assert host.send_terminal("session-id", "pwd\n") is False

    terminal._daemon_controller.send_input.assert_not_called()
    terminal.backend.feed_child_data.assert_not_called()


def test_pty_autofill_does_not_log_secret(caplog):
    t = _term(response="super-secret-value")
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        t._on_pty_autofill_changed(None)
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "super-secret-value" not in joined
    assert t._fed == [b"super-secret-value\n"]
