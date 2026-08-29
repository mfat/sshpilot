from sshpilot.api import Capability
from sshpilot.daemon.dispatch import DAEMON_METHOD_CAPABILITIES
from sshpilot.daemon.launcher import DaemonLauncher
from sshpilot.core.connection_application_service import (
    ConnectionApplicationService, IMPLEMENTED_CLIENT_METHOD_CAPABILITIES,
)


def test_connection_service_only_declares_connection_operations():
    assert set(IMPLEMENTED_CLIENT_METHOD_CAPABILITIES) == {
        "close", "get_capabilities", "get_connection", "get_connection_editor",
        "get_ssh_config_text", "get_effective_config", "check_unsaved_host", "prepare_external_terminal_launch", "get_launch_command",
        "save_ssh_config_text",
        "list_connections", "create_connection", "duplicate_connection",
        "delete_connection", "store_connection_password",
        "set_session_connection_password",
        "clear_session_connection_password",
        "delete_connection_password",
        "store_key_passphrase", "delete_key_passphrase",
        "update_connection_metadata", "add_tag_to_connections", "assign_connection_to_group", "move_connections", "create_group",
        "delete_group", "rename_group", "split_connection", "subscribe_events",
        "update_connection", "get_global_ssh_overrides",
        "update_global_ssh_overrides", "reset_global_ssh_overrides",
    }
    for client_only in ("open_session", "open_sftp", "open_forward", "start_transfer"):
        assert not hasattr(ConnectionApplicationService, client_only)


def test_daemon_dispatch_covers_all_backend_domains():
    capabilities = {cap for cap in DAEMON_METHOD_CAPABILITIES.values() if cap}
    required = {
        Capability.CONNECTIONS_READ, Capability.CONNECTIONS_WRITE,
        Capability.SESSIONS_READ, Capability.SESSIONS_WRITE,
        Capability.TERMINAL_INPUT, Capability.TERMINAL_RESIZE,
        Capability.TERMINAL_REPLAY, Capability.INTERACTIONS_RESPOND,
        Capability.SFTP_READ, Capability.SFTP_WRITE,
        Capability.TRANSFERS_READ, Capability.TRANSFERS_WRITE,
        Capability.FORWARDS_READ, Capability.FORWARDS_WRITE,
        Capability.DAEMON_STATUS, Capability.DAEMON_CONTROL,
    }
    assert required <= capabilities


def test_launcher_validates_capabilities_before_returning_client():
    source = DaemonLauncher._connect.__code__.co_names
    assert "get_capabilities" in source
    assert "MISSING_CAPABILITY" in source


def test_update_request_accepts_empty_port_as_the_clear_signal():
    """`""` clears an authored Port; anything else non-numeric stays invalid."""
    import pytest
    from sshpilot.api.models.connections import UpdateConnectionRequest

    assert UpdateConnectionRequest(port="").port == ""
    assert UpdateConnectionRequest(port=2200).port == 2200
    for bad in ("22a", "abc", 0, 70000):
        with pytest.raises(ValueError):
            UpdateConnectionRequest(port=bad)
