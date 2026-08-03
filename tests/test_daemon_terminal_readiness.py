"""Daemon terminal readiness — separate from route selection."""

from unittest.mock import Mock

from sshpilot.api.capabilities import Capability
from sshpilot.api.version import PROTOCOL_VERSION
from sshpilot.daemon_terminal_policy import (
    DaemonTerminalReadinessReason,
    resolve_daemon_terminal_readiness,
    resolve_ssh_terminal_route,
    SshTerminalRoute,
)
from sshpilot.terminal_session_controller import (
    required_daemon_terminal_capabilities,
)


REQUIRED = required_daemon_terminal_capabilities()


def _capabilities(supported, *, protocol=PROTOCOL_VERSION, compatible=True):
    caps = Mock()
    caps.protocol_version = protocol
    caps.supported = frozenset(supported)
    caps.compatibility = Mock(compatible=compatible)
    return caps


def _daemon_client(supported=REQUIRED):
    client = Mock()
    client.server_instance_id = "daemon-instance"
    client.open_session = Mock()
    client.get_capabilities = Mock(return_value=_capabilities(supported))
    return client


def test_fully_ready():
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(), bridge=Mock()
    )
    assert readiness.ready
    assert readiness.reason is None


def test_client_absent():
    readiness = resolve_daemon_terminal_readiness(None, bridge=Mock())
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE


def test_bridge_absent():
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(), bridge=None
    )
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.BRIDGE_UNAVAILABLE


def test_missing_server_instance_id_attribute():
    client = Mock(spec=["open_session", "get_capabilities"])
    client.open_session = Mock()
    readiness = resolve_daemon_terminal_readiness(client, bridge=Mock())
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE


def test_handshake_incomplete_raises():
    client = Mock()
    type(client).server_instance_id = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("incomplete"))
    )
    client.open_session = Mock()
    readiness = resolve_daemon_terminal_readiness(client, bridge=Mock())
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.HANDSHAKE_INCOMPLETE


def test_empty_server_instance_id():
    client = _daemon_client()
    client.server_instance_id = ""
    readiness = resolve_daemon_terminal_readiness(client, bridge=Mock())
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.SERVER_INSTANCE_MISSING


def test_protocol_mismatch():
    client = _daemon_client()
    client.get_capabilities = Mock(
        return_value=_capabilities(REQUIRED, protocol="0.9")
    )
    readiness = resolve_daemon_terminal_readiness(client, bridge=Mock())
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.PROTOCOL_INCOMPATIBLE


def test_missing_terminal_capability():
    supported = REQUIRED - {Capability.TERMINAL_OUTPUT}
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(supported), bridge=Mock()
    )
    assert not readiness.ready
    assert (
        readiness.reason
        is DaemonTerminalReadinessReason.TERMINAL_TRANSPORT_UNAVAILABLE
    )
    assert Capability.TERMINAL_OUTPUT in readiness.missing_capabilities


def test_missing_secret_transport():
    supported = REQUIRED - {Capability.INTERACTIONS_PASSWORD}
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(supported), bridge=Mock()
    )
    assert not readiness.ready
    assert (
        readiness.reason
        is DaemonTerminalReadinessReason.SECRET_TRANSPORT_UNAVAILABLE
    )


def test_missing_interaction_capability_mixed():
    supported = REQUIRED - {
        Capability.SESSIONS_WRITE,
        Capability.INTERACTIONS_EVENTS,
    }
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(supported), bridge=Mock()
    )
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.MISSING_CAPABILITIES


def test_daemon_start_failure_reason():
    readiness = resolve_daemon_terminal_readiness(
        None,
        bridge=Mock(),
        startup_failure=DaemonTerminalReadinessReason.DAEMON_START_FAILED,
    )
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.DAEMON_START_FAILED


def test_daemon_start_timeout_reason():
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(),
        bridge=Mock(),
        startup_failure=DaemonTerminalReadinessReason.DAEMON_START_TIMEOUT,
    )
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.DAEMON_START_TIMEOUT


def test_selection_pending():
    readiness = resolve_daemon_terminal_readiness(
        _daemon_client(),
        bridge=Mock(),
        selection_pending=True,
    )
    assert not readiness.ready
    assert readiness.reason is DaemonTerminalReadinessReason.SELECTION_PENDING


def test_readiness_failure_does_not_change_route():
    from types import SimpleNamespace

    config = SimpleNamespace(
        get_setting=lambda key, default=None: {
            "use-external-terminal": False,
            "terminal.removed_local_ssh_setting": False,
            "terminal.daemon_backed_ssh": True,
        }.get(key, default)
    )
    connection = SimpleNamespace(protocol="ssh")
    route = resolve_ssh_terminal_route(config, connection)
    readiness = resolve_daemon_terminal_readiness(None, bridge=None)
    assert route is SshTerminalRoute.DAEMON
    assert not readiness.ready
