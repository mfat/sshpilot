"""SSH terminal route resolution — policy only, no readiness."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sshpilot.daemon_terminal_policy import (
    DAEMON_BACKED_SSH_SETTING,
    DEFAULT_DAEMON_BACKED_SSH,
    LEGACY_LOCAL_SSH_FALLBACK_SETTING,
    SshTerminalRoute,
    resolve_ssh_terminal_route,
    should_use_daemon_ssh_terminal,
)


def _config(settings: dict):
    return SimpleNamespace(
        get_setting=lambda key, default=None: settings.get(key, default)
    )


def _ssh_connection(protocol: str = "ssh"):
    return SimpleNamespace(protocol=protocol, nickname="Demo")


@pytest.mark.parametrize(
    "external,legacy,daemon,expected",
    [
        (True, False, True, SshTerminalRoute.EXTERNAL),
        (True, True, True, SshTerminalRoute.EXTERNAL),
        (False, True, True, SshTerminalRoute.LEGACY_LOCAL),
        (False, False, True, SshTerminalRoute.DAEMON),
        (False, False, False, SshTerminalRoute.LEGACY_LOCAL),
    ],
)
def test_route_matrix(external, legacy, daemon, expected):
    config = _config(
        {
            "use-external-terminal": external,
            LEGACY_LOCAL_SSH_FALLBACK_SETTING: legacy,
            DAEMON_BACKED_SSH_SETTING: daemon,
        }
    )
    with patch(
        "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
        return_value=False,
    ):
        assert (
            resolve_ssh_terminal_route(config, _ssh_connection()) is expected
        )


def test_external_hidden_by_policy_falls_through():
    config = _config(
        {
            "use-external-terminal": True,
            LEGACY_LOCAL_SSH_FALLBACK_SETTING: False,
            DAEMON_BACKED_SSH_SETTING: True,
        }
    )
    with patch(
        "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
        return_value=True,
    ):
        assert (
            resolve_ssh_terminal_route(config, _ssh_connection())
            is SshTerminalRoute.DAEMON
        )


def test_local_terminal_not_an_ssh_route():
    config = _config({DAEMON_BACKED_SSH_SETTING: True})
    assert resolve_ssh_terminal_route(config, _ssh_connection(), is_local=True) is None


def test_non_ssh_protocol_not_routed():
    config = _config({DAEMON_BACKED_SSH_SETTING: True})
    assert resolve_ssh_terminal_route(config, _ssh_connection("docker")) is None


def test_missing_connection_not_routed():
    config = _config({DAEMON_BACKED_SSH_SETTING: True})
    assert resolve_ssh_terminal_route(config, None) is None


def test_invalid_setting_values_use_defaults():
    config = _config({})
    with patch(
        "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
        return_value=False,
    ):
        assert (
            resolve_ssh_terminal_route(config, _ssh_connection())
            is SshTerminalRoute.DAEMON
        )
    assert DEFAULT_DAEMON_BACKED_SSH is True


def test_route_ignores_client_readiness():
    """Absent/broken client must not change the selected route."""

    config = _config(
        {
            "use-external-terminal": False,
            LEGACY_LOCAL_SSH_FALLBACK_SETTING: False,
            DAEMON_BACKED_SSH_SETTING: True,
        }
    )
    window = SimpleNamespace(config=config, client=None)
    with patch(
        "sshpilot.daemon_terminal_policy.should_hide_external_terminal_options",
        return_value=False,
    ):
        assert (
            resolve_ssh_terminal_route(window, _ssh_connection())
            is SshTerminalRoute.DAEMON
        )
        assert should_use_daemon_ssh_terminal(
            window, _ssh_connection(), client=None
        )
