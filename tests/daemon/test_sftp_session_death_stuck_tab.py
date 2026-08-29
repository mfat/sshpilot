"""A live SFTP connection dying mid-session must not leave the file-manager
tab silently stuck.

Wires the real production ``SftpServiceRuntime`` (daemon/server side)
directly to a real ``DaemonSftpServiceController`` (client/file-manager-tab
side), with only the actual OpenSSH sftp subprocess replaced by a fake
process handle -- the same boundary ``tests/daemon/test_sftp_runtime.py``
already fakes for server-only lifecycle tests.

Regression coverage for an incident where a router dropped off the network
mid-session: the underlying network recovered on its own and new SFTP/
terminal connections to the same host worked again, but the already-open
file manager tab kept failing operations and never recovered until the
whole app was restarted.

Root cause (fixed in two layers): a live SFTP process had no reaper, so a
mid-session death was only discoverable on the next filesystem operation.
The runner now polls the ``ssh … -s sftp`` child the same way terminal
sessions do, and ``_map_error`` still treats an unambiguous connection-loss
error (``ErrorCode.SFTP_PROTOCOL_LOST`` -- a raw ``EOFError``/``OSError``,
e.g. a broken pipe, or an SFTP-protocol ``FX_NO_CONNECTION`` /
``FX_CONNECTION_LOST`` reply) as service death if the reaper has not
already flipped the record. Deliberately NOT included: a generic
SFTP-protocol ``FX_FAILURE`` reply (e.g. "directory not empty") -- that is
a legitimate per-request error, not proof the whole service is dead.
That FAILED event reaches ``DaemonSftpServiceController._on_open_accepted``,
which fires ``on_error`` -> ``DaemonSftpManager``'s ``connection-error`` ->
``FileManagerWindow``'s Retry UI, and every subsequent op on that tab is now
rejected locally (``SFTP_SERVICE_NOT_READY``) instead of hitting the dead
connection again.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import OpenSftpRequest, SftpServiceState
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime
from sshpilot.sftp_service_controller import (
    DaemonSftpServiceController,
    SftpControllerState,
    required_daemon_sftp_capabilities,
)


class _Connection:
    def __init__(self):
        self.id = ConnectionId("demo")
        self.protocol = "ssh"
        self.hostname = "example.test"
        self.username = "alice"
        self.port = 22


class _CoreClient:
    def __init__(self):
        self.connection = _Connection()

    def get_connection(self, _connection_id):
        return self.connection


class _Attr:
    def __init__(self, name, is_dir=False):
        self.filename = name
        self._is_dir = is_dir
        self.st_mode = 0o040755 if is_dir else 0o100644
        self.st_size = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_mtime = None

    def is_dir(self):
        return self._is_dir

    def is_symlink(self):
        return False


class _FlakySftpClient:
    """Real-shaped SFTP client stub: healthy, then dead -- like a router
    drop mid-session. Nothing raises on the transport itself when it dies;
    only the next command fails, exactly like a real severed session."""

    def __init__(self):
        self.alive = True
        self.cwd = "/"

    def realpath(self, path):
        return self.cwd if path == "." else path

    def listdir_attr(self, path):
        if not self.alive:
            # A severed SSH connection surfaces as the local sftp
            # subprocess's pipe breaking -- a raw OSError, the same
            # exception class a real BrokenPipeError/ConnectionResetError
            # (both OSError subclasses) would raise. This is what
            # _map_error's (EOFError, OSError) branch -- and therefore
            # _fail_dead_connection -- is built to catch. A generic
            # SFTP-protocol FX_FAILURE reply (e.g. "directory not empty")
            # is deliberately NOT treated as connection death: it is a
            # legitimate per-request error, not a dead service.
            raise OSError("Connection reset by peer")
        return [_Attr("file.txt")]

    def close(self):
        pass


class _FakeSftpHandle:
    def __init__(self, client):
        self.client = client

    def terminate(self):
        self.client.close()

    def wait(self, timeout):
        del timeout
        return True


class _FakeSftpRunner:
    def __init__(self, client):
        self._client = client
        self.on_exit = None

    def start(self, spec, on_exit=None):
        del spec
        self.on_exit = on_exit
        return _FakeSftpHandle(self._client)

    def close(self):
        pass


class _SyncBridge:
    """Runs the op synchronously -- stands in for the real GLib-marshalled
    daemon-client bridge, the same role a Mock bridge plays in
    tests/test_sftp_service_controller.py."""

    def submit(self, factory, *, on_success, on_error):
        try:
            result = factory()
        except BaseException as exc:  # noqa: BLE001
            on_error(exc)
        else:
            on_success(result)


def _open_ready(runtime, owner):
    """Open+start a fresh service on the real runtime and return its id."""
    summary = runtime.prepare_open_service(
        OpenSftpRequest(connection_id=ConnectionId("demo")), client_id=owner
    )
    runtime.start_service(summary.id)
    assert runtime.get_service(summary.id).state is SftpServiceState.READY
    return summary.id


def test_dead_connection_fails_service_and_signals_the_tab():
    owner = ClientId("client:owner")

    # ---- Server side: the real SftpServiceRuntime, real state machine ----
    sftp_client = _FlakySftpClient()
    runtime = SftpServiceRuntime(_CoreClient(), runner=_FakeSftpRunner(sftp_client))
    _open_ready(runtime, owner)

    # ---- Client side: the real DaemonSftpServiceController, glued
    # directly to the real runtime above (the mock client's methods call
    # straight into runtime methods -- same call shape a real DaemonClient
    # RPC produces). ----
    mock_client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_sftp_capabilities()
    mock_client.get_capabilities.return_value = capabilities
    mock_client.open_sftp.side_effect = lambda req: runtime.prepare_open_service(
        req, client_id=owner
    )
    mock_client.sftp_list_directory.side_effect = lambda req: runtime.list_directory(
        req, client_id=owner
    )
    # Wire the controller's event subscription straight to the real
    # runtime's real publisher -- state-change events (READY, FAILED, ...)
    # now reach the controller exactly as the daemon's sftp.* event stream
    # would, with no manual event injection.
    mock_client.subscribe_events.side_effect = runtime.subscribe_events

    controller = DaemonSftpServiceController(
        client=mock_client, bridge=_SyncBridge(), connection_id=ConnectionId("demo")
    )
    errors = []
    controller._on_error = errors.append

    controller.open()
    runtime.start_service(controller.service_id)
    assert controller.state is SftpControllerState.READY

    # ---- The router drops off the network. No background health check
    # notices; the daemon only finds out when the next op is attempted. ----
    sftp_client.alive = False

    op_errors = []
    controller.list_directory(
        "/tmp", on_success=lambda _r: None, on_error=op_errors.append
    )
    assert len(op_errors) == 1
    assert "The SFTP connection was lost" in str(op_errors[0])

    # Fixed: the failed op flips the service record to FAILED instead of
    # leaving it READY forever.
    assert runtime.get_service(controller.service_id).state is SftpServiceState.FAILED

    # The FAILED event reached the real controller synchronously (the
    # _SyncBridge runs the op inline, and _map_error publishes before
    # returning), so the tab already knows it is dead.
    assert controller.state is SftpControllerState.FAILED
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY

    # Further ops are now rejected locally -- no more requests hit the dead
    # connection, unlike the old behavior of failing the same way forever.
    more_errors = []
    controller.list_directory(
        "/tmp", on_success=lambda _r: None, on_error=more_errors.append
    )
    assert len(more_errors) == 1
    assert more_errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
    assert len(errors) == 1  # no duplicate connection-error signal


def test_reconnect_after_dead_connection_recovers_the_tab():
    """The Retry affordance (FileManagerWindow._reconnect) re-opens the
    controller; once the network is back this must succeed and restore
    normal operation, matching the real incident where new sessions to the
    same host worked again once the router came back."""
    owner = ClientId("client:owner")

    sftp_client = _FlakySftpClient()
    runtime = SftpServiceRuntime(_CoreClient(), runner=_FakeSftpRunner(sftp_client))
    _open_ready(runtime, owner)

    mock_client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_sftp_capabilities()
    mock_client.get_capabilities.return_value = capabilities
    mock_client.open_sftp.side_effect = lambda req: runtime.prepare_open_service(
        req, client_id=owner
    )
    mock_client.sftp_list_directory.side_effect = lambda req: runtime.list_directory(
        req, client_id=owner
    )
    mock_client.subscribe_events.side_effect = runtime.subscribe_events

    controller = DaemonSftpServiceController(
        client=mock_client, bridge=_SyncBridge(), connection_id=ConnectionId("demo")
    )
    controller.open()
    runtime.start_service(controller.service_id)
    assert controller.state is SftpControllerState.READY

    sftp_client.alive = False
    controller.list_directory("/tmp", on_success=lambda _r: None, on_error=lambda _e: None)
    assert controller.state is SftpControllerState.FAILED

    # The router comes back (real logs: new sessions to the same host on
    # the same daemon worked again shortly after). Retry opens a fresh
    # service on a fresh (now-healthy) client.
    sftp_client.alive = True
    controller.open()
    runtime.start_service(controller.service_id)
    assert controller.state is SftpControllerState.READY

    results = []
    controller.list_directory(
        "/tmp", on_success=results.append, on_error=lambda e: pytest.fail(str(e))
    )
    assert len(results) == 1


def test_process_exit_fails_the_tab_without_an_operation():
    """The ssh child exiting must surface Retry immediately, like a terminal."""
    owner = ClientId("client:owner")
    sftp_client = _FlakySftpClient()
    runner = _FakeSftpRunner(sftp_client)
    runtime = SftpServiceRuntime(_CoreClient(), runner=runner)
    _open_ready(runtime, owner)

    mock_client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_sftp_capabilities()
    mock_client.get_capabilities.return_value = capabilities
    mock_client.open_sftp.side_effect = lambda req: runtime.prepare_open_service(
        req, client_id=owner
    )
    mock_client.sftp_list_directory.side_effect = lambda req: runtime.list_directory(
        req, client_id=owner
    )
    mock_client.subscribe_events.side_effect = runtime.subscribe_events

    errors = []
    controller = DaemonSftpServiceController(
        client=mock_client,
        bridge=_SyncBridge(),
        connection_id=ConnectionId("demo"),
        on_error=errors.append,
    )
    controller.open()
    runtime.start_service(controller.service_id)
    assert controller.state is SftpControllerState.READY
    assert runner.on_exit is not None

    runner.on_exit(255)

    assert controller.state is SftpControllerState.FAILED
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.SFTP_SERVICE_NOT_READY
