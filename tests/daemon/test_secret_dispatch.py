"""Daemon secret RPC tests: dispatch routing, capability advertisement,
codec wire mapping, InProcessClient parity and sentinel secrecy through the
serialized transport surface.

Sentinel secrets are the same values the service tests use; if any of them
ever appears in a wire payload, a serialized response, an error detail or a
capability advertisement, the secrecy contract is broken.
"""

from __future__ import annotations

import json
from typing import Any, List
from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.transport.codec import (
    secret_configuration_from_wire,
    secret_configuration_to_wire,
    secret_operation_result_from_wire,
    secret_operation_result_to_wire,
    secret_transfer_result_from_wire,
    secret_transfer_result_to_wire,
    secret_unlock_result_from_wire,
    secret_unlock_result_to_wire,
)
from sshpilot.api.transport.envelopes import HandshakeRequest, RequestEnvelope
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon.dispatch import (
    ClientProtocolState,
    DeferredResult,
    RequestDispatcher,
    DAEMON_METHOD_CAPABILITIES,
    DEFERRED_DAEMON_METHODS,
    DRAIN_REJECTED_METHODS,
)

SENTINEL = "SENTINEL_MASTER_9f2a"

_request_counter = [0]


def _envelope(method, params):
    _request_counter[0] += 1
    return RequestEnvelope(
        protocol_version="1.0",
        request_id=RequestId(f"req-{_request_counter[0]}"),
        method=method,
        params=params,
        client_id=ClientId("client-1"),
    )


def _state() -> ClientProtocolState:
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
    state.selected_protocol_version = "1.0"
    return state


def _dispatcher(*, with_service=True):
    from sshpilot.api.models.secrets import (
        BitwardenStatus,
        SecretBackendState,
        SecretConfiguration,
    )

    connections = ConnectionApplicationService(mock.Mock(), client_name="test")
    service = mock.Mock() if with_service else None
    if service is not None:
        service.get_configuration.return_value = SecretConfiguration(
            revision="abc123", backend="auto", session_timeout=0,
            remember_in_keyring=False, bitwarden_profile="", bitwarden_server="",
            keepassxc_database="", keepassxc_keyfile="",
        )
        service.get_registry.return_value = mock.Mock(
            backends=(), effective_backend="none", selected_backend="auto"
        )
        service.get_state.return_value = SecretBackendState(
            selected_backend="auto", effective_backend="none", locked=False,
            needs_unlock=False, login_required=False, session_timeout=0,
            remember_in_keyring=False, persists_secrets=True,
        )
        service.bitwarden_status.return_value = BitwardenStatus(
            logged_in=False, unlocked=False, needs_login=True, email="",
            server_url="", profile="", twofa_required=False, message="",
        )
    return RequestDispatcher(connections, secrets_service=service), service


# ---------------------------------------------------------------------------
# Method mapping + capability advertisement
# ---------------------------------------------------------------------------

def test_secret_methods_map_to_operate_capability():
    for method in (
        "secrets.unlock",
        "secrets.lock",
        "secrets.bitwarden.status",
        "secrets.bitwarden.configure_server",
        "secrets.bitwarden.login",
        "secrets.bitwarden.api_key_login",
        "secrets.bitwarden.sso_login",
        "secrets.bitwarden.unlock",
        "secrets.bitwarden.sync",
        "secrets.bitwarden.lock",
        "secrets.bitwarden.logout",
        "secrets.rbw.status",
        "secrets.rbw.configure",
        "secrets.rbw.unlock",
        "secrets.rbw.sync",
        "secrets.rbw.lock",
        "secrets.keepassxc.create_database",
        "secrets.keepassxc.unlock",
        "secrets.keepassxc.lock",
        "secrets.remember_master_password",
        "secrets.forget_master_password",
    ):
        assert DAEMON_METHOD_CAPABILITIES[method] is Capability.SECRETS_OPERATE
        assert method in DEFERRED_DAEMON_METHODS

    assert (
        DAEMON_METHOD_CAPABILITIES["secrets.configuration.get"]
        is Capability.SECRETS_READ
    )
    assert (
        DAEMON_METHOD_CAPABILITIES["secrets.configuration.update"]
        is Capability.SECRETS_WRITE
    )
    assert (
        DAEMON_METHOD_CAPABILITIES["secrets.selection.update"]
        is Capability.SECRETS_WRITE
    )
    assert (
        DAEMON_METHOD_CAPABILITIES["secrets.transfer.export"]
        is Capability.SECRETS_TRANSFER
    )
    assert (
        DAEMON_METHOD_CAPABILITIES["secrets.transfer.import"]
        is Capability.SECRETS_TRANSFER
    )


def test_connection_secret_methods_are_deferred_and_capability_mapped():
    """Every connections.* secret handler returns a DeferredResult, so each
    method must be in DEFERRED_DAEMON_METHODS — otherwise dispatch rejects it
    with an opaque INTERNAL_ERROR (see issue #1136, where
    ``connections.delete_passphrase`` was missing)."""
    for method in (
        "connections.store_password",
        "connections.delete_password",
        "connections.store_passphrase",
        "connections.delete_passphrase",
        "connections.store_plugin_secret",
        "connections.delete_plugin_secret",
    ):
        assert DAEMON_METHOD_CAPABILITIES[method] is Capability.CONNECTIONS_SECRETS_WRITE
        assert method in DEFERRED_DAEMON_METHODS, method
    for method in (
        "connections.has_password",
        "connections.has_passphrase",
    ):
        assert (
            DAEMON_METHOD_CAPABILITIES[method]
            is Capability.CONNECTIONS_SECRETS_STATUS_READ
        )
        assert method in DEFERRED_DAEMON_METHODS, method
    for method in (
        "connections.reveal_password",
        "connections.reveal_passphrase",
        "connections.get_plugin_secret",
    ):
        assert DAEMON_METHOD_CAPABILITIES[method] is Capability.CONNECTIONS_SECRETS_REVEAL
        assert method in DEFERRED_DAEMON_METHODS, method


def test_secret_handlers_are_registered():
    dispatcher, _ = _dispatcher()
    for method in (
        "secrets.configuration.get",
        "secrets.configuration.update",
        "secrets.backends.get",
        "secrets.state.get",
        "secrets.selection.update",
        "secrets.unlock",
        "secrets.lock",
        "secrets.bitwarden.status",
        "secrets.bitwarden.configure_server",
        "secrets.bitwarden.login",
        "secrets.bitwarden.api_key_login",
        "secrets.bitwarden.sso_login",
        "secrets.bitwarden.unlock",
        "secrets.bitwarden.sync",
        "secrets.bitwarden.lock",
        "secrets.bitwarden.logout",
        "secrets.rbw.status",
        "secrets.rbw.configure",
        "secrets.rbw.unlock",
        "secrets.rbw.sync",
        "secrets.rbw.lock",
        "secrets.keepassxc.create_database",
        "secrets.keepassxc.unlock",
        "secrets.keepassxc.lock",
        "secrets.remember_master_password",
        "secrets.forget_master_password",
        "secrets.transfer.export",
        "secrets.transfer.import",
    ):
        assert method in dispatcher.HANDLERS, method


def test_secrets_capability_advertised_only_with_service():
    dispatcher, _ = _dispatcher(with_service=True)
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.SECRETS_READ in capabilities.supported
    assert Capability.SECRETS_OPERATE in capabilities.supported

    dispatcher_none, _ = _dispatcher(with_service=False)
    capabilities_none = dispatcher_none._capabilities_for(_state())
    assert Capability.SECRETS_READ not in capabilities_none.supported
    assert Capability.SECRETS_OPERATE not in capabilities_none.supported
    assert Capability.SECRETS_TRANSFER not in capabilities_none.supported


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------

def test_configuration_get_requires_empty_parameters():
    dispatcher, service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("secrets.configuration.get", {"x": 1}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_configuration_get_returns_deferred_result():
    dispatcher, service = _dispatcher()
    result = dispatcher.dispatch(_envelope("secrets.configuration.get", {}), _state())
    assert isinstance(result, DeferredResult)
    wire = result.operation()
    assert wire["revision"] == "abc123"


def test_configuration_update_requires_request_params():
    dispatcher, service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("secrets.configuration.update", {}), _state()
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_missing_service_raises_unsupported_capability():
    dispatcher, _ = _dispatcher(with_service=False)
    for method in ("secrets.state.get", "secrets.unlock", "secrets.backends.get"):
        with pytest.raises(SshPilotError) as excinfo:
            dispatcher.dispatch(_envelope(method, {}), _state())
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    # Transfer methods (valid params) still reject on missing service.
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "secrets.transfer.export",
                {
                    "destination": "/tmp/x.spbk",
                    "connection_ids": [],
                    "options": {},
                    "mirror_logins": False,
                },
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_bitwarden_status_delegates_to_service():
    dispatcher, service = _dispatcher()
    result = dispatcher.dispatch(
        _envelope("secrets.bitwarden.status", {}), _state()
    )
    assert isinstance(result, DeferredResult)
    result.operation()
    service.bitwarden_status.assert_called_once()


def test_selection_update_passes_expected_revision():
    from sshpilot.api.models.secrets import SecretBackendState

    dispatcher, service = _dispatcher()
    service.update_selection.return_value = SecretBackendState(
        selected_backend="bitwarden", effective_backend="bitwarden", locked=True,
        needs_unlock=True, login_required=True, session_timeout=0,
        remember_in_keyring=False, persists_secrets=True,
    )
    result = dispatcher.dispatch(
        _envelope(
            "secrets.selection.update",
            {"backend": "bitwarden", "expected_revision": "abc123"},
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    wire = result.operation()
    service.update_selection.assert_called_once_with(
        "bitwarden", expected_revision="abc123"
    )
    assert wire["selected_backend"] == "bitwarden"


def test_draining_rejects_operate_but_allows_reads():
    assert "secrets.unlock" in DRAIN_REJECTED_METHODS
    assert "secrets.transfer.export" in DRAIN_REJECTED_METHODS
    assert "secrets.configuration.get" not in DRAIN_REJECTED_METHODS
    assert "secrets.backends.get" not in DRAIN_REJECTED_METHODS


# ---------------------------------------------------------------------------
# Codec wire mapping (no secret values in wire form)
# ---------------------------------------------------------------------------

def _strings(value: Any, out: List[str]) -> List[str]:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _strings(v, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out)
    return out


# ---------------------------------------------------------------------------
# Backup preview routing + secret-free transport
# ---------------------------------------------------------------------------

def test_preview_methods_map_to_transfer_capability():
    for method in ("secrets.transfer.preview", "secrets.transfer.preview_bitwarden",
                   "secrets.transfer.preview_ssh"):
        assert DAEMON_METHOD_CAPABILITIES[method] is Capability.SECRETS_TRANSFER
        assert method in DEFERRED_DAEMON_METHODS
        assert method in DRAIN_REJECTED_METHODS


def test_preview_handlers_are_registered():
    dispatcher, _ = _dispatcher()
    for method in ("secrets.transfer.preview", "secrets.transfer.preview_bitwarden",
                   "secrets.transfer.preview_ssh"):
        assert method in dispatcher.HANDLERS, method


def test_preview_backup_delegates_to_service():
    dispatcher, service = _dispatcher()
    service.preview_backup.return_value = {
        "kind": "spbk", "encrypted": False,
        "included": {"app_settings": True, "ssh_config": True, "known_hosts": False,
                     "secrets": True, "private_keys": False},
    }
    result = dispatcher.dispatch(
        _envelope("secrets.transfer.preview", {"source": "/tmp/x.spbk"}), _state())
    assert isinstance(result, DeferredResult)
    wire = result.operation()
    assert wire["kind"] == "spbk"
    assert wire["included"]["secrets"] is True
    service.preview_backup.assert_called_once_with(source="/tmp/x.spbk")


def test_preview_bitwarden_and_ssh_delegate_to_service():
    dispatcher, service = _dispatcher()
    service.preview_bitwarden_backup.return_value = {"kind": "bitwarden", "included": {}}
    service.preview_ssh_backup.return_value = {"kind": "ssh", "included": {}}

    r1 = dispatcher.dispatch(
        _envelope("secrets.transfer.preview_bitwarden", {"entry_id": "e1"}), _state())
    assert r1.operation()["kind"] == "bitwarden"
    service.preview_bitwarden_backup.assert_called_once_with(entry_id="e1")

    r2 = dispatcher.dispatch(
        _envelope(
            "secrets.transfer.preview_ssh",
            {"connection_id": "srv", "remote_dir": "~/bk", "entry_id": "e2"},
        ),
        _state(),
    )
    assert r2.operation()["kind"] == "ssh"
    service.preview_ssh_backup.assert_called_once_with(
        connection_id="srv", remote_dir="~/bk", entry_id="e2")


def test_preview_params_are_validated():
    dispatcher, _ = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("secrets.transfer.preview", {}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("secrets.transfer.preview_bitwarden", {"entry_id": ""}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("secrets.transfer.preview_ssh", {"connection_id": "x"}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_real_daemon_preview_backup_returns_metadata_only(tmp_path):
    """``preview_backup`` over a real daemon returns kind/encryption/included —
    never manifest content (no ``credentials`` / ``private_keys`` keys)."""
    from sshpilot.core.settings import save_settings
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.secret_backend_service import SecretBackendService
    from sshpilot.daemon.server import CoreServices
    from tests.helpers.fake_connection_repository import make_test_connection_service

    socket_path = tmp_path / "secrets-preview.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_path = tmp_path / "config.json"
    save_settings(settings_path, {"config_version": 3})
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"ssh": {}}), encoding="utf-8")

    fake = _FakeSecretManager()

    def _build():
        connections = make_test_connection_service(client_name="sshpilotd")
        return CoreServices(
            connections=connections,
            secrets=SecretBackendService(settings_path, secret_manager=fake),
        )

    server = DaemonServer(_build, socket_path=socket_path)
    server.start_in_thread()
    try:
        client = DaemonClient(socket_path=socket_path)
        try:
            preview = client.preview_backup(source=str(source))
            assert preview["kind"] == "json"
            assert preview["encrypted"] is False
            assert set(preview) <= {"kind", "encrypted", "included", "error"}
        finally:
            client.close()
    finally:
        server.shutdown()
        server.wait_stopped()


def test_secret_configuration_wire_roundtrip():
    from sshpilot.api.models.secrets import SecretConfiguration

    config = SecretConfiguration(
        revision="rev-1",
        backend="keepassxc",
        session_timeout=15,
        remember_in_keyring=True,
        bitwarden_profile="/home/u/.config/bw",
        bitwarden_server="https://vault.example.com",
        keepassxc_database="/home/u/vault.kdbx",
        keepassxc_keyfile="/home/u/vault.key",
    )
    wire = secret_configuration_to_wire(config)
    assert SENTINEL not in " ".join(_strings(wire, []))
    assert secret_configuration_from_wire(wire) == config


def test_secret_unlock_result_wire_roundtrip():
    from sshpilot.api.models.secrets import SecretUnlockResult, UnlockResultKind

    result = SecretUnlockResult(
        kind=UnlockResultKind.UNLOCKED, backend="bitwarden", message=""
    )
    wire = secret_unlock_result_to_wire(result)
    assert secret_unlock_result_from_wire(wire) == result


def test_secret_operation_result_wire_roundtrip():
    from sshpilot.api.models.secrets import SecretOperationResult, SecretOperationState

    result = SecretOperationResult(
        state=SecretOperationState.SUCCESS,
        backend="keepassxc",
        message="",
    )
    wire = secret_operation_result_to_wire(result)
    assert secret_operation_result_from_wire(wire) == result


def test_secret_transfer_result_wire_roundtrip_and_no_secrets():
    from sshpilot.api.models.secrets import SecretOperationState, SecretTransferResult

    result = SecretTransferResult(
        operation="export",
        path="/home/u/backup.spbk",
        counts={"credentials": 2, "private_keys": 1},
        warnings=("one SSH config file skipped",),
        status=SecretOperationState.SUCCESS,
        message="",
    )
    wire = secret_transfer_result_to_wire(result)
    assert secret_transfer_result_from_wire(wire) == result
    assert SENTINEL not in " ".join(_strings(wire, []))


# ---------------------------------------------------------------------------
# Real-daemon transport parity + secret-free RPC params
# ---------------------------------------------------------------------------

def test_real_daemon_secret_configuration_roundtrip(tmp_path):
    """``get/update_secret_configuration`` round-trip over a real daemon with
    the secrets service installed (wire decode, revision semantics)."""
    from sshpilot.api.models.secrets import (
        SecretConfiguration,
        UpdateSecretConfigurationRequest,
    )
    from sshpilot.core.settings import save_settings
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.secret_backend_service import SecretBackendService
    from sshpilot.daemon.server import CoreServices
    from tests.helpers.fake_connection_repository import make_test_connection_service

    socket_path = tmp_path / "secrets.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_path = tmp_path / "config.json"
    save_settings(settings_path, {"config_version": 3})

    fake = _FakeSecretManager()

    def _build():
        connections = make_test_connection_service(client_name="sshpilotd")
        return CoreServices(
            connections=connections,
            secrets=SecretBackendService(
                settings_path, secret_manager=fake
            ),
        )

    server = DaemonServer(_build, socket_path=socket_path)
    server.start_in_thread()
    try:
        client = DaemonClient(socket_path=socket_path)
        try:
            result = client.get_secret_configuration()
            assert type(result) is SecretConfiguration
            assert result.backend == "auto"

            updated = client.update_secret_configuration(
                UpdateSecretConfigurationRequest(
                    patch={"session_timeout": 30},
                    expected_revision=result.revision,
                )
            )
            assert updated.session_timeout == 30
            assert updated.revision != result.revision
        finally:
            client.close()
    finally:
        server.shutdown()
        server.wait_stopped()


def test_real_daemon_login_params_are_identifiers_only(tmp_path):
    """The login RPC carries only the email identifier — no master password.
    The backend raises if a password were supplied (it isn't), and the wire
    request never contains secret material."""
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.secret_backend_service import SecretBackendService
    from sshpilot.daemon.server import CoreServices
    from tests.helpers.fake_connection_repository import make_test_connection_service

    class _CancelBroker:
        """No secrets are ever entered: the protected interaction is cancelled."""

        def create(self, **kwargs):
            return mock.Mock(id="inter-cancel")

        def wait_for_result(self, interaction_id):
            return None

    socket_path = tmp_path / "secrets2.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_path = tmp_path / "config2.json"
    fake = _FakeSecretManager(backends={"bitwarden": _FakeBwBackend()})

    def _build():
        connections = make_test_connection_service(client_name="sshpilotd")
        return CoreServices(
            connections=connections,
            secrets=SecretBackendService(
                settings_path,
                secret_manager=fake,
                interaction_broker=_CancelBroker(),
            ),
        )

    server = DaemonServer(_build, socket_path=socket_path)
    server.start_in_thread()
    try:
        # The daemon constructs its own interaction broker at startup; replace
        # it so the protected interaction resolves immediately to "cancelled"
        # instead of blocking the RPC waiting for a secret frame.
        server._secrets_service.attach_interaction_broker(_CancelBroker())
        client = DaemonClient(socket_path=socket_path)
        try:
            status = client.bitwarden_login("alice@example.com")
            # The interaction was cancelled -> no password entered, status is
            # safe and the wire request carried only the email identifier.
            assert status.email == "alice@example.com"
            assert status.needs_login is True
            assert SENTINEL not in status.message
            assert SENTINEL not in json.dumps(status.to_dict())
        finally:
            client.close()
    finally:
        server.shutdown()
        server.wait_stopped()


class _FakeSecretManager:
    """Minimal SecretManager for the real-daemon test composition."""

    def __init__(self, backends=None):
        self._backends = backends or {}
        self._selected = "auto"
        self.active_backend_name = "none"

    def registered_backends(self):
        return list(self._backends)

    def available_backends(self, *, cheap=False):
        return list(self._backends)

    def get_backend(self, name):
        return self._backends.get((name or "").strip().lower())

    def set_selected(self, name):
        self._selected = name or "auto"

    def selected_backend(self):
        return self._backends.get(self._selected) if self._selected != "auto" else None

    def persists_secrets(self):
        return True

    def unlock_selected(self, secret, progress=None):
        return True

    def store_in_keyring(self, spec, secret):
        return True

    def lookup_in_keyring(self, spec):
        return None

    def delete_in_keyring(self, spec):
        return True


class _FakeBwBackend:
    name = "bitwarden"

    def is_available(self):
        return True

    def is_unlocked(self):
        return False

    def needs_login(self, *, force_refresh=False):
        return True

    def login_with_password(self, email, password, *, twofa_method=None,
                            twofa_code=None, auth_client_secret=None):
        # The service must never hand a real master password to the backend
        # through this RPC; it collects it via a protected interaction instead.
        if password and SENTINEL in password:
            raise AssertionError("master password crossed the RPC surface")
        return False, "Login failed.", False

    def login_with_api_key(self, client_id, client_secret):
        return False, ""

    def login_with_sso(self, identifier=None):
        return False, ""

    def lock(self):
        pass

    def logout(self):
        pass
