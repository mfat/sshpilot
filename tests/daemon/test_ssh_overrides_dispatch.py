"""SSH overrides daemon dispatch RPC tests."""

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.transport.envelopes import HandshakeRequest, RequestEnvelope
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.daemon.dispatch import (
    ClientProtocolState,
    RequestDispatcher,
    DAEMON_METHOD_CAPABILITIES,
    DEFERRED_DAEMON_METHODS,
    DRAIN_REJECTED_METHODS,
)

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


def _dispatcher(tmp_path, *, with_service=True):
    connections = ConnectionApplicationService(mock.Mock(), client_name="test")
    service = (
        SshOverridesService(tmp_path / "config.json")
        if with_service
        else None
    )
    return RequestDispatcher(
        connections, ssh_overrides_service=service
    ), service


# ---------------------------------------------------------------------------
# Method mapping
# ---------------------------------------------------------------------------
def test_methods_map_to_capabilities():
    assert (
        DAEMON_METHOD_CAPABILITIES["ssh_overrides.get"]
        is Capability.SSH_OVERRIDES_READ
    )
    assert (
        DAEMON_METHOD_CAPABILITIES["ssh_overrides.update"]
        is Capability.SSH_OVERRIDES_WRITE
    )
    assert (
        DAEMON_METHOD_CAPABILITIES["ssh_overrides.reset"]
        is Capability.SSH_OVERRIDES_WRITE
    )
    # Writes are rejected while draining; reads remain available.
    assert "ssh_overrides.get" not in DRAIN_REJECTED_METHODS
    assert "ssh_overrides.update" in DRAIN_REJECTED_METHODS
    assert "ssh_overrides.reset" in DRAIN_REJECTED_METHODS
    # Immediate (synchronous) methods, not deferred work.
    assert "ssh_overrides.get" not in DEFERRED_DAEMON_METHODS
    assert "ssh_overrides.update" not in DEFERRED_DAEMON_METHODS
    assert "ssh_overrides.reset" not in DEFERRED_DAEMON_METHODS


def test_handlers_are_registered(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    for method in (
        "ssh_overrides.get",
        "ssh_overrides.update",
        "ssh_overrides.reset",
    ):
        assert method in dispatcher.HANDLERS


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------
def test_installed_service_advertises_ssh_overrides(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.SSH_OVERRIDES_READ in capabilities.supported
    assert Capability.SSH_OVERRIDES_WRITE in capabilities.supported


def test_missing_service_does_not_advertise_ssh_overrides(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path, with_service=False)
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.SSH_OVERRIDES_READ not in capabilities.supported
    assert Capability.SSH_OVERRIDES_WRITE not in capabilities.supported


# ---------------------------------------------------------------------------
# get RPC
# ---------------------------------------------------------------------------
def test_get_returns_wire_snapshot(tmp_path):
    dispatcher, service = _dispatcher(tmp_path)
    result = dispatcher.dispatch(_envelope("ssh_overrides.get", {}), _state())
    assert set(result.value) == {
        "revision",
        "connect_timeout",
        "connection_attempts",
        "server_alive_interval",
        "server_alive_count_max",
        "strict_host_key_checking",
        "batch_mode",
        "compression",
        "verbosity",
        "debug_enabled",
        "applies_immediately",
    }
    assert result.value["strict_host_key_checking"] == "accept-new"


def test_get_requires_empty_params(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("ssh_overrides.get", {"x": 1}), _state()
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_get_without_service_raises_unsupported(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path, with_service=False)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("ssh_overrides.get", {}), _state())
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_get_available_while_draining(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    dispatcher.begin_shutdown()
    result = dispatcher.dispatch(_envelope("ssh_overrides.get", {}), _state())
    assert result.value["connect_timeout"] == 0


# ---------------------------------------------------------------------------
# update RPC
# ---------------------------------------------------------------------------
def test_update_persists_through_wire(tmp_path):
    dispatcher, service = _dispatcher(tmp_path)
    before = dispatcher.dispatch(_envelope("ssh_overrides.get", {}), _state()).value
    result = dispatcher.dispatch(
        _envelope(
            "ssh_overrides.update",
            {
                "patch": {"connect_timeout": 12},
                "expected_revision": before["revision"],
            },
        ),
        _state(),
    )
    assert result.value["connect_timeout"] == 12
    assert result.value["revision"] != before["revision"]

    on_disk = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "ConnectTimeout=12" in on_disk


def test_update_rejects_stale_revision(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "ssh_overrides.update",
                {"patch": {"connect_timeout": 12}, "expected_revision": "stale"},
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_update_rejects_unknown_patch_field(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "ssh_overrides.update",
                {"patch": {"bogus": 1}},
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_update_rejected_while_draining(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    dispatcher.begin_shutdown()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("ssh_overrides.update", {"patch": {}}), _state()
        )
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN


# ---------------------------------------------------------------------------
# reset RPC
# ---------------------------------------------------------------------------
def test_reset_restores_defaults(tmp_path):
    dispatcher, service = _dispatcher(tmp_path)
    before = dispatcher.dispatch(_envelope("ssh_overrides.get", {}), _state()).value
    changed = dispatcher.dispatch(
        _envelope(
            "ssh_overrides.update",
            {"patch": {"verbosity": 2}, "expected_revision": before["revision"]},
        ),
        _state(),
    ).value
    assert changed["verbosity"] == 2

    reset = dispatcher.dispatch(
        _envelope(
            "ssh_overrides.reset",
            {"expected_revision": changed["revision"]},
        ),
        _state(),
    ).value
    assert reset["verbosity"] == 0
    assert reset["connect_timeout"] == 0


def test_reset_rejects_unknown_params(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "ssh_overrides.reset",
                {"expected_revision": "abc", "surprise": 1},
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_reset_rejects_malformed_expected_revision(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("ssh_overrides.reset", {"expected_revision": ""}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_reset_rejects_stale_revision(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("ssh_overrides.reset", {"expected_revision": "stale"}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_reset_rejected_while_draining(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    dispatcher.begin_shutdown()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("ssh_overrides.reset", {}), _state())
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
