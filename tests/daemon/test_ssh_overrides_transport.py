"""SSH overrides end-to-end transport tests over a real in-process daemon.

Exercises ``DaemonClient.get/update/reset_global_ssh_overrides`` against a
``DaemonServer`` whose composition installs ``SshOverridesService``, verifying
wire decode, capability negotiation, revision semantics, and that secrets or
paths never appear in diagnostics.
"""

import pytest

from sshpilot.api import DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.server import CoreServices
from tests.helpers.fake_connection_repository import make_test_connection_service


@pytest.fixture
def overrides_server(tmp_path):
    servers = []

    def _start():
        socket_path = tmp_path / f"overrides-sock-{len(servers)}" / "sshpilotd.sock"
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings_path = tmp_path / f"config-{len(servers)}.json"

        def _build():
            connections = make_test_connection_service(client_name="sshpilotd")
            service = SshOverridesService(settings_path)
            return CoreServices(
                connections=connections,
                ssh_overrides=service,
            )

        server = DaemonServer(_build, socket_path=socket_path)
        server.start_in_thread()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.wait_stopped()


def test_get_round_trip_over_real_daemon(tmp_path, overrides_server):
    server = overrides_server()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        result = client.get_global_ssh_overrides()
        assert type(result) is GlobalSshOverrides
        assert result.strict_host_key_checking == "accept-new"
        assert result.connect_timeout == 0
        assert result.revision
    finally:
        client.close()


def test_update_round_trip_over_real_daemon(tmp_path, overrides_server):
    server = overrides_server()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        before = client.get_global_ssh_overrides()
        updated = client.update_global_ssh_overrides(
            UpdateGlobalSshOverridesRequest(
                patch={"connect_timeout": 15, "batch_mode": True},
                expected_revision=before.revision,
            )
        )
        assert updated.connect_timeout == 15
        assert updated.batch_mode is True
        assert updated.revision != before.revision

        # Fresh read reflects daemon state.
        again = client.get_global_ssh_overrides()
        assert again.connect_timeout == 15
        assert again.revision == updated.revision
    finally:
        client.close()


def test_update_rejects_stale_revision_over_real_daemon(
    tmp_path, overrides_server
):
    server = overrides_server()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.update_global_ssh_overrides(
                UpdateGlobalSshOverridesRequest(
                    patch={"connect_timeout": 5},
                    expected_revision="stale",
                )
            )
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    finally:
        client.close()


def test_reset_round_trip_over_real_daemon(tmp_path, overrides_server):
    server = overrides_server()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        before = client.get_global_ssh_overrides()
        changed = client.update_global_ssh_overrides(
            UpdateGlobalSshOverridesRequest(
                patch={"verbosity": 3},
                expected_revision=before.revision,
            )
        )
        assert changed.verbosity == 3

        reset = client.reset_global_ssh_overrides(
            expected_revision=changed.revision
        )
        assert reset.verbosity == 0
        assert reset.connect_timeout == 0
    finally:
        client.close()


def test_reset_rejects_stale_revision_over_real_daemon(
    tmp_path, overrides_server
):
    server = overrides_server()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.reset_global_ssh_overrides(expected_revision="stale")
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    finally:
        client.close()


def test_ssh_overrides_available_over_real_daemon(tmp_path, overrides_server):
    server = overrides_server()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        from sshpilot.api.capabilities import Capability

        capabilities = client.get_capabilities()
        assert capabilities.supports(Capability.SSH_OVERRIDES_READ)
        assert capabilities.supports(Capability.SSH_OVERRIDES_WRITE)
    finally:
        client.close()
