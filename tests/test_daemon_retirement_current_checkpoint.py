"""Permanent regressions for the post-0.40 daemon-only contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_frontend_reload_operation_preserves_a_newer_daemon_value(tmp_path):
    """A canonical reload must refresh both visible data and merge baseline."""
    from sshpilot.config import Config

    path = tmp_path / "config.json"
    initial = {
        "config_version": 3,
        "ui": {"language": "en"},
        "plugins": {"docker_enabled": False},
    }
    path.write_text(json.dumps(initial), encoding="utf-8")
    config = Config.__new__(Config)
    config.config_file = str(path)
    config.config_data = copy.deepcopy(initial)
    config._config_snapshot = copy.deepcopy(initial)

    daemon_tree = copy.deepcopy(initial)
    daemon_tree["plugins"]["docker_enabled"] = True
    path.write_text(json.dumps(daemon_tree), encoding="utf-8")
    config.reload_json_cache_strict()

    newer_daemon_tree = copy.deepcopy(daemon_tree)
    newer_daemon_tree["plugins"]["docker_enabled"] = "daemon-newer"
    path.write_text(json.dumps(newer_daemon_tree), encoding="utf-8")
    config.config_data["ui"]["language"] = "fa"
    assert config.save_json_config()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ui"]["language"] == "fa"
    assert saved["plugins"]["docker_enabled"] == "daemon-newer"


def test_import_reload_uses_canonical_cache_reload():
    """No production caller may assign a freshly loaded tree directly."""
    source = Path("src/sshpilot/window_dialogs.py").read_text(encoding="utf-8")
    assert "config.config_data = self.config.load_json_config()" not in source


def test_recovery_status_does_not_confirm_or_apply_operation_mode():
    pytest.importorskip("gi")
    from sshpilot.api.models.daemon import OperationMode
    from sshpilot.window import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.client = SimpleNamespace(
        get_capabilities=lambda: SimpleNamespace(
            supports=lambda _capability: True
        ),
        get_operation_mode=lambda: None,
    )
    window.client_bridge = MagicMock()
    window._confirmed_operation_mode = None
    window._requested_operation_mode = None
    window._apply_confirmed_operation_mode = MagicMock()
    window._show_operation_mode_recovery = MagicMock()
    window.client_bridge.submit.side_effect = lambda operation, **callbacks: callbacks[
        "on_success"
    ](
        SimpleNamespace(
            accepted=False,
            recovery_required=True,
            active_mode=OperationMode.ISOLATED,
            message="runtime and persisted modes differ",
        )
    )

    window._refresh_operation_mode_scope()

    window._apply_confirmed_operation_mode.assert_not_called()
    window._show_operation_mode_recovery.assert_called_once_with(
        "runtime and persisted modes differ"
    )
    assert window._confirmed_operation_mode is None


def test_reconnect_resets_open_preferences_mode_confirmation():
    pytest.importorskip("gi")
    from sshpilot.api.models.daemon import OperationMode
    from sshpilot.window import MainWindow

    old_client = object()
    new_client = object()
    preferences = MagicMock()
    preferences._confirmed_operation_mode = OperationMode.ISOLATED
    window = MainWindow.__new__(MainWindow)
    window.client = old_client
    window._confirmed_operation_mode = OperationMode.DEFAULT
    window.key_manager = None
    window._group_mutation_controller = None
    window._secrets_interaction_presenter = None
    window._preferences_window = preferences
    window._attach_client_backed_services = MagicMock()
    window._build_ssh_overrides_controller = MagicMock(return_value=object())

    window._replace_daemon_client(new_client)

    assert window.client is new_client
    preferences.reset_operation_mode_confirmation.assert_called_once_with()


def test_reconnect_rebinds_live_terminals_to_the_new_client():
    """A daemon transport replacement must push the new client into every open
    terminal's session controller — otherwise a deferred callback for an
    in-flight terminal (e.g. session-opened racing the old transport's
    shutdown) still calls through the closed old client and raises
    "The client is closed" uncaught (observed as a crash while a terminal
    sits stuck at "connecting")."""
    pytest.importorskip("gi")
    from sshpilot.window import MainWindow
    from sshpilot.terminal_manager import TerminalManager

    old_client = object()
    new_client = object()
    preferences = None
    window = MainWindow.__new__(MainWindow)
    window.client = old_client
    window._confirmed_operation_mode = None
    window.key_manager = None
    window._group_mutation_controller = None
    window._secrets_interaction_presenter = None
    window._preferences_window = preferences
    window._attach_client_backed_services = MagicMock()
    window.terminal_manager = TerminalManager(window)

    stale_terminal = MagicMock()
    window.active_terminals = {"conn": stale_terminal}
    window.connection_to_terminals = {}
    window.tab_view = None

    window._replace_daemon_client(new_client)

    assert window.client is new_client
    stale_terminal.rebind_daemon_client.assert_called_once_with(new_client)


def test_reconnect_rebind_failure_does_not_publish_new_selection():
    from sshpilot import main as main_module
    from sshpilot.api.daemon_reconnect import DaemonReconnectResult
    from sshpilot.daemon.launcher import DaemonLaunchResult
    from sshpilot.daemon.reconnect import ReconnectDecision, ReconnectOutcome

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    old_client = MagicMock()
    new_client = MagicMock()
    app._api_client_selection = SimpleNamespace(client=old_client, daemon_process=None)
    app._api_client_bridge = None
    app._daemon_reconnect_in_progress = True
    app._daemon_reconnect_generation = 0
    app._daemon_shutdown_intent = None
    app.window = SimpleNamespace(
        _is_quitting=False,
        _replace_daemon_client=MagicMock(side_effect=RuntimeError("rebind failed")),
    )
    app.install_api_event_subscription = MagicMock()
    result = DaemonReconnectResult(
        client=new_client,
        launched=DaemonLaunchResult(client=new_client, process=None),
        decision=ReconnectDecision(outcome=ReconnectOutcome.ATTEMPT, message="ok"),
    )

    assert app._finish_daemon_reconnect(result) is False
    assert app._api_client_selection.client is old_client
    new_client.close.assert_called_once_with()


def test_missing_persisted_mode_after_failed_transition_requires_recovery(tmp_path):
    from sshpilot.api.models.daemon import OperationMode, SetOperationModeRequest
    from sshpilot.daemon.operation_mode_service import OperationModeService

    repository = MagicMock()
    repository.ssh_config_isolated = False
    repository.snapshot.return_value.generation = 1
    service = OperationModeService(
        repository,
        config_path=tmp_path / "config.json",
        default_root=tmp_path / "default-config",
        isolated_root=tmp_path / "isolated-config",
        default_state_path=tmp_path / "connections.json",
        isolated_state_path=tmp_path / "connections-isolated.json",
    )
    service._prepare_target = MagicMock(side_effect=OSError("target unavailable"))

    result = service.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))

    assert result.accepted is False
    assert result.recovery_required is True
    assert result.persisted_mode is None
    assert "recovery" in result.message.lower()


def test_launcher_preserves_api_mismatch_as_restart_required():
    from sshpilot.api.errors import DaemonRestartRequiredError
    from sshpilot.daemon.launcher import DaemonStartupFailure, DaemonLauncher

    reason = DaemonLauncher._classify_handshake_error(
        DaemonRestartRequiredError("restart required")
    )

    assert reason.reason is DaemonStartupFailure.API_VERSION_MISMATCH
