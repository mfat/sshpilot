"""SSH overrides public contract tests for direct core and daemon paths."""

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api.capabilities import Capability
from sshpilot.api.client import SshPilotClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.daemon_client import DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES
from sshpilot.api.method_capabilities import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
from sshpilot.api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)
from sshpilot.core.ssh_overrides_service import SshOverridesService
from tests.helpers.fake_connection_repository import make_test_repository


def test_protocol_declares_ssh_overrides_methods():
    for method_name in (
        "get_global_ssh_overrides",
        "update_global_ssh_overrides",
        "reset_global_ssh_overrides",
    ):
        assert callable(getattr(SshPilotClient, method_name))


def test_daemon_client_registers_ssh_overrides_methods():
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["get_global_ssh_overrides"]
        is Capability.SSH_OVERRIDES_READ
    )
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["update_global_ssh_overrides"]
        is Capability.SSH_OVERRIDES_WRITE
    )
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["reset_global_ssh_overrides"]
        is Capability.SSH_OVERRIDES_WRITE
    )
    # No longer schema-only on the daemon client.
    for name in (
        "get_global_ssh_overrides",
        "update_global_ssh_overrides",
        "reset_global_ssh_overrides",
    ):
        assert name not in UNSUPPORTED_CLIENT_METHOD_CAPABILITIES


def _direct_core_service(tmp_path, *, with_service=True):
    service = (
        SshOverridesService(tmp_path / "config.json")
        if with_service
        else None
    )
    return ConnectionApplicationService(
        make_test_repository(),
        ssh_overrides=service,
        client_name="test",
    )


def _full_snapshot_values(snapshot):
    return {
        "connect_timeout": snapshot.connect_timeout,
        "connection_attempts": snapshot.connection_attempts,
        "server_alive_interval": snapshot.server_alive_interval,
        "server_alive_count_max": snapshot.server_alive_count_max,
        "strict_host_key_checking": snapshot.strict_host_key_checking,
        "batch_mode": snapshot.batch_mode,
        "compression": snapshot.compression,
        "verbosity": snapshot.verbosity,
        "debug_enabled": snapshot.debug_enabled,
    }


def test_direct_core_service_reads_global_overrides(tmp_path):
    client = _direct_core_service(tmp_path)
    snapshot = client.get_global_ssh_overrides()
    assert isinstance(snapshot, GlobalSshOverrides)
    assert snapshot.connect_timeout == 0
    assert snapshot.strict_host_key_checking == "accept-new"


def test_direct_core_service_updates_global_overrides(tmp_path):
    client = _direct_core_service(tmp_path)
    before = client.get_global_ssh_overrides()
    updated = client.update_global_ssh_overrides(
        UpdateGlobalSshOverridesRequest(
            patch={"connect_timeout": 30, "batch_mode": True},
            expected_revision=before.revision,
        )
    )
    assert updated.connect_timeout == 30
    assert updated.batch_mode is True
    assert updated.revision != before.revision

    # A second read reflects the same daemon-owned state.
    again = client.get_global_ssh_overrides()
    assert _full_snapshot_values(again) == _full_snapshot_values(updated)


def test_direct_core_service_reset_restores_defaults(tmp_path):
    client = _direct_core_service(tmp_path)
    before = client.get_global_ssh_overrides()
    changed = client.update_global_ssh_overrides(
        UpdateGlobalSshOverridesRequest(
            patch={"server_alive_interval": 45},
            expected_revision=before.revision,
        )
    )
    assert changed.server_alive_interval == 45
    reset = client.reset_global_ssh_overrides(
        expected_revision=changed.revision
    )
    assert reset.server_alive_interval == 0
    assert reset.batch_mode is False


def test_direct_core_service_rejects_stale_revision(tmp_path):
    client = _direct_core_service(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        client.update_global_ssh_overrides(
            UpdateGlobalSshOverridesRequest(
                patch={"connect_timeout": 5},
                expected_revision="stale",
            )
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_direct_core_service_without_service_raises_canonical_unsupported(tmp_path):
    client = _direct_core_service(tmp_path, with_service=False)
    with pytest.raises(SshPilotError) as excinfo:
        client.get_global_ssh_overrides()
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_direct_core_service_without_service_never_advertises(tmp_path):
    client = _direct_core_service(tmp_path, with_service=False)
    capabilities = client.get_capabilities()
    assert not capabilities.supports(Capability.SSH_OVERRIDES_READ)
    assert not capabilities.supports(Capability.SSH_OVERRIDES_WRITE)


def test_direct_core_service_with_service_advertises(tmp_path):
    client = _direct_core_service(tmp_path, with_service=True)
    capabilities = client.get_capabilities()
    assert capabilities.supports(Capability.SSH_OVERRIDES_READ)
    assert capabilities.supports(Capability.SSH_OVERRIDES_WRITE)


def test_direct_core_update_rejects_wrong_request_type(tmp_path):
    client = _direct_core_service(tmp_path)
    with pytest.raises(TypeError):
        client.update_global_ssh_overrides({"connect_timeout": 5})
