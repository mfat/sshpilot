"""Daemon SSH-key service composition tests."""

from unittest import mock

from sshpilot.api.models.common import ClientId
from sshpilot.api.models.keys import KeyStoreScope
from sshpilot.api.transport.envelopes import HandshakeRequest
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon.dispatch import ClientProtocolState, RequestDispatcher
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.server import CoreServices
from sshpilot.platform.paths import get_config_dir, get_ssh_dir


def _call_production_composition(tmp_path, monkeypatch):
    """Run ``cli._production_core_services`` against isolated headless paths.

    The composition reads no GI adapters, so no module faking is needed; we
    isolate ``SSHPILOT_SSH_DIR`` and ``XDG_CONFIG_HOME`` so the headless
    repository never touches a developer's real config.
    """
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(tmp_path / "ssh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from sshpilot.daemon import cli

    return cli._production_core_services()


def test_production_composition_installs_key_service(tmp_path, monkeypatch):
    services = _call_production_composition(tmp_path, monkeypatch)
    assert isinstance(services.keys, DaemonKeyService)
    assert services.keys is not None


def test_production_resolver_uses_headless_path_helpers(monkeypatch, tmp_path):
    override = tmp_path / "ssh"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(override))
    services = _call_production_composition(tmp_path, monkeypatch)
    resolver = services.keys._path_resolver
    assert resolver(KeyStoreScope.DEFAULT) == get_ssh_dir() == override
    assert resolver(KeyStoreScope.ISOLATED) == get_config_dir()


def test_test_services_may_omit_keys():
    services = CoreServices(
        connections=ConnectionApplicationService(mock.Mock(), client_name="test")
    )
    assert services.keys is None


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


def test_dispatcher_without_service_advertises_no_key_capability():
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test")
    )
    capabilities = dispatcher._capabilities_for(_handshake_state())
    values = {item.value for item in capabilities.supported}
    assert "keys.read" not in values
    assert "keys.write" not in values


def test_dispatcher_with_service_advertises_key_capabilities():
    service = DaemonKeyService(lambda scope: mock.Mock())
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test"),
        key_service=service,
    )
    capabilities = dispatcher._capabilities_for(_handshake_state())
    values = {item.value for item in capabilities.supported}
    assert "keys.read" in values
    assert "keys.write" in values
