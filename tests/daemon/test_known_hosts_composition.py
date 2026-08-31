"""Daemon known-hosts service composition tests."""

import sys
from unittest import mock

from sshpilot.api.models.common import ClientId
from sshpilot.api.transport.envelopes import HandshakeRequest
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon.dispatch import ClientProtocolState, RequestDispatcher
from sshpilot.daemon.known_hosts_service import KnownHostsService
from sshpilot.daemon.server import CoreServices
from sshpilot.platform.paths import get_ssh_dir


def _call_production_composition():
    """Run ``cli._production_core_services`` with GI adapters faked out."""

    fake_config = mock.Mock()
    fake_manager = mock.Mock()
    fake_manager.identity_migration_error = None
    fake_groups = mock.Mock()
    fake_modules = {
        "sshpilot.config": mock.Mock(Config=lambda: fake_config),
        "sshpilot.connection_manager": mock.Mock(
            ConnectionManager=lambda config: fake_manager
        ),
        "sshpilot.groups": mock.Mock(
            GroupManager=lambda config, connection_manager: fake_groups
        ),
        "sshpilot.plugins.loader": mock.Mock(load_plugins=mock.Mock()),
    }
    with mock.patch.dict(sys.modules, fake_modules):
        from sshpilot.daemon import cli

        return cli._production_core_services()


def test_production_composition_installs_known_hosts_service():
    services = _call_production_composition()
    assert isinstance(services.known_hosts, KnownHostsService)
    assert services.known_hosts is not None


def test_production_resolver_uses_headless_path_helper(monkeypatch, tmp_path):
    override = tmp_path / "ssh"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(override))
    services = _call_production_composition()
    assert services.known_hosts._path_resolver() == get_ssh_dir() / "known_hosts"
    assert services.known_hosts._path_resolver() == override / "known_hosts"


def test_resolver_follows_a_live_operation_mode_switch(monkeypatch, tmp_path):
    """The known-hosts editor must edit the active root's file.

    It was hardwired to ~/.ssh/known_hosts in both modes, so in Isolated Mode
    the editor showed -- and deleted from -- the user's global trust store
    while the sessions it was supposed to describe used a different scope
    entirely. The resolver is called per request, so a live switch is picked
    up without rebuilding the service.
    """
    from sshpilot.api.models.daemon import OperationMode, SetOperationModeRequest
    from sshpilot.platform.paths import get_config_dir

    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host web\n    HostName web.example\n")
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(ssh_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    services = _call_production_composition()
    resolve = services.known_hosts._path_resolver

    assert resolve() == ssh_dir / "known_hosts"

    services.operation_mode.apply(SetOperationModeRequest(mode=OperationMode.ISOLATED))
    assert resolve() == get_config_dir() / "known_hosts"

    services.operation_mode.apply(SetOperationModeRequest(mode=OperationMode.DEFAULT))
    assert resolve() == ssh_dir / "known_hosts"


def test_test_services_may_omit_known_hosts():
    services = CoreServices(
        connections=ConnectionApplicationService(mock.Mock(), client_name="test")
    )
    assert services.known_hosts is None


def _handshake_state() -> ClientProtocolState:
    state = ClientProtocolState()
    state.handshake_completed = True
    state.client_id = ClientId("client-1")
    state.client_info = HandshakeRequest(
        client_name="test",
        client_version="1.0",
        supported_protocol_versions=("1.0",),
        client_capabilities=frozenset(),
        frontend_type="cli",
        supported_frame_types=frozenset(),
    )
    return state


def test_dispatcher_without_service_advertises_no_known_hosts_capability():
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test")
    )
    capabilities = dispatcher._capabilities_for(_handshake_state())
    values = {item.value for item in capabilities.supported}
    assert "known_hosts.read" not in values
    assert "known_hosts.write" not in values
