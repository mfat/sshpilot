"""SFTP file read/replace daemon dispatch RPC tests."""

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId, RequestId, utc_now
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
    SftpCopyRequest,
    SftpDirectorySizeRequest,
    SftpDirectorySizeResult,
    SftpFileTarget,
    SftpPathRequest,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
)
from sshpilot.api.transport.codec import (
    sftp_path_request_from_wire,
    sftp_path_request_to_wire,
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


class _FakeSftpRuntime:
    def __init__(self):
        self.read_calls = []
        self.replace_calls = []
        self.copy_calls = []
        self.remove_calls = []
        self.directory_size_calls = []

    def directory_size(self, request, client_id=None):
        self.directory_size_calls.append(request)
        return SftpDirectorySizeResult(
            path=request.path,
            size_bytes=42,
            file_count=2,
            directory_count=1,
        )

    def _operation(self, kind, message):
        return OperationSummary(
            operation_id=OperationId("operation-1"),
            kind=kind,
            state=OperationState.QUEUED,
            message=message,
            created_at=utc_now(),
            connection_id=ConnectionId("conn-1"),
            owner_client_id=ClientId("client-1"),
        )

    def start_directory_size(self, request, client_id=None):
        self.directory_size_calls.append(request)
        return self._operation(
            OperationKind.SFTP_DIRECTORY_SIZE, f"Measuring {request.path}"
        )

    def start_copy(self, request, client_id=None):
        self.copy_calls.append(request)
        return self._operation(
            OperationKind.SFTP_COPY_TREE, f"Copying {request.source_path}"
        )

    def start_remove(self, request, client_id=None):
        self.remove_calls.append(request)
        return self._operation(
            OperationKind.SFTP_REMOVE_TREE, f"Deleting {request.path}"
        )

    def read_file(self, request, client_id=None):
        self.read_calls.append(request)
        return SftpReadFileResult(
            target=request.target,
            path=request.path,
            content="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIone\n",
            exists=True,
            revision="rev-1",
            size=42,
            mode=0o600,
        )

    def replace_file(self, request, client_id=None):
        self.replace_calls.append(request)
        return SftpReplaceFileResult(
            target=request.target,
            path=request.path,
            revision="rev-2",
            size=42,
            backup_path=request.path + ".bak",
        )

    def copy(self, request, client_id=None):
        self.copy_calls.append(request)

    def remove(self, request, client_id=None):
        self.remove_calls.append(request)


def _dispatcher(with_runtime=True):
    connections = ConnectionApplicationService(mock.Mock(), client_name="test")
    runtime = _FakeSftpRuntime() if with_runtime else None
    return RequestDispatcher(connections, sftp_runtime=runtime), runtime


# ---------------------------------------------------------------------------
# Method mapping
# ---------------------------------------------------------------------------
def test_read_file_maps_to_read_capability():
    assert DAEMON_METHOD_CAPABILITIES["sftp.read_file"] is Capability.SFTP_READ
    assert "sftp.read_file" in DEFERRED_DAEMON_METHODS
    assert "sftp.read_file" not in DRAIN_REJECTED_METHODS


def test_replace_file_maps_to_mutate_capability():
    assert DAEMON_METHOD_CAPABILITIES["sftp.replace_file"] is Capability.SFTP_MUTATE
    assert "sftp.replace_file" in DEFERRED_DAEMON_METHODS
    assert "sftp.replace_file" in DRAIN_REJECTED_METHODS


def test_copy_maps_to_mutate_capability():
    assert DAEMON_METHOD_CAPABILITIES["sftp.copy"] is Capability.SFTP_MUTATE
    assert "sftp.copy" in DEFERRED_DAEMON_METHODS
    assert "sftp.copy" in DRAIN_REJECTED_METHODS


def test_directory_size_maps_to_read_capability():
    assert DAEMON_METHOD_CAPABILITIES["sftp.directory_size"] is Capability.SFTP_READ
    assert "sftp.directory_size" in DEFERRED_DAEMON_METHODS
    assert "sftp.directory_size" not in DRAIN_REJECTED_METHODS


def test_directory_size_starts_daemon_operation():
    dispatcher, runtime = _dispatcher()
    result = dispatcher.dispatch(
        _envelope(
            "sftp.directory_size",
            {"service_id": "sftp-1", "path": "/tree"},
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert result.command_key == "sftp-1"
    wire = result.operation()
    assert wire["operation_id"] == "operation-1"
    assert wire["kind"] == OperationKind.SFTP_DIRECTORY_SIZE.value
    assert wire["state"] == OperationState.QUEUED.value
    assert isinstance(runtime.directory_size_calls[0], SftpDirectorySizeRequest)
    assert runtime.directory_size_calls[0].path == "/tree"


def test_file_handlers_are_registered():
    dispatcher, _runtime = _dispatcher()
    assert "sftp.read_file" in dispatcher.HANDLERS
    assert "sftp.replace_file" in dispatcher.HANDLERS
    assert "sftp.copy" in dispatcher.HANDLERS


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------
def test_read_file_returns_deferred_execution():
    dispatcher, runtime = _dispatcher()
    result = dispatcher.dispatch(
        _envelope(
            "sftp.read_file",
            {
                "target": SftpFileTarget.REMOTE.value,
                "path": "/home/user/.ssh/authorized_keys",
                "service_id": "sftp-1",
            },
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert result.command_key == "sftp-1"
    wire = result.operation()
    assert wire["exists"] is True
    assert wire["content"].startswith("ssh-ed25519")
    assert wire["path"] == "/home/user/.ssh/authorized_keys"
    assert isinstance(runtime.read_calls[0], SftpReadFileRequest)
    assert runtime.read_calls[0].target is SftpFileTarget.REMOTE


def test_read_file_local_target_uses_local_command_key():
    dispatcher, runtime = _dispatcher()
    result = dispatcher.dispatch(
        _envelope(
            "sftp.read_file",
            {
                "target": SftpFileTarget.LOCAL_AUTHORIZED_KEYS.value,
                "path": "~/.ssh/authorized_keys",
            },
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert result.command_key == "local-authorized-keys"
    wire = result.operation()
    assert wire["exists"] is True
    assert runtime.read_calls[0].service_id is None


def test_copy_returns_deferred_execution():
    dispatcher, runtime = _dispatcher()
    result = dispatcher.dispatch(
        _envelope(
            "sftp.copy",
            {
                "service_id": "sftp-1",
                "source_path": "/source",
                "destination_path": "/destination",
                "recursive": True,
                "move": True,
            },
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert result.command_key == "sftp-1"
    wire = result.operation()
    assert wire["operation_id"] == "operation-1"
    assert wire["kind"] == OperationKind.SFTP_COPY_TREE.value
    request = runtime.copy_calls[0]
    assert isinstance(request, SftpCopyRequest)
    assert request.recursive is True
    assert request.move is True


def test_path_request_recursive_round_trips_through_wire():
    request = SftpPathRequest(service_id="sftp-1", path="/tree", recursive=True)
    wire = sftp_path_request_to_wire(request)
    assert wire["recursive"] is True
    decoded = sftp_path_request_from_wire(wire)
    assert decoded.recursive is True
    assert decoded.path == "/tree"
    assert sftp_path_request_from_wire(
        {"service_id": "sftp-1", "path": "/tree"}
    ).recursive is False


@pytest.mark.parametrize(
    "bad_value",
    [
        "false",
        "true",
        "0",
        "1",
        0,
        1,
        None,
        [],
        {},
    ],
)
def test_path_request_recursive_rejects_non_boolean_wire_values(bad_value):
    with pytest.raises(ValueError):
        sftp_path_request_from_wire(
            {
                "service_id": "sftp-1",
                "path": "/tree",
                "recursive": bad_value,
            }
        )


def test_remove_rejects_non_boolean_recursive_wire_value():
    dispatcher, _runtime = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "sftp.remove",
                {
                    "service_id": "sftp-1",
                    "path": "/tree",
                    "recursive": "false",
                },
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_remove_recursive_passes_flag_through_wire():
    dispatcher, runtime = _dispatcher()
    result = dispatcher.dispatch(
        _envelope(
            "sftp.remove",
            {
                "service_id": "sftp-1",
                "path": "/tree",
                "recursive": True,
            },
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    wire = result.operation()
    assert wire["operation_id"] == "operation-1"
    assert wire["kind"] == OperationKind.SFTP_REMOVE_TREE.value
    request = runtime.remove_calls[0]
    assert request.recursive is True
    assert request.path == "/tree"


def test_replace_file_returns_deferred_execution():
    dispatcher, runtime = _dispatcher()
    result = dispatcher.dispatch(
        _envelope(
            "sftp.replace_file",
            {
                "target": SftpFileTarget.REMOTE.value,
                "path": "/home/user/.ssh/authorized_keys",
                "content": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIone\n",
                "expected_revision": "rev-1",
                "backup": True,
                "service_id": "sftp-1",
            },
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert result.command_key == "sftp-1"
    wire = result.operation()
    assert wire["revision"] == "rev-2"
    assert wire["path"] == "/home/user/.ssh/authorized_keys"
    assert isinstance(runtime.replace_calls[0], SftpReplaceFileRequest)


def test_read_file_malformed_request_is_rejected():
    dispatcher, _runtime = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("sftp.read_file", {"path": "x"}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_read_file_missing_runtime_is_unsupported():
    dispatcher, _runtime = _dispatcher(with_runtime=False)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "sftp.read_file",
                {
                    "target": SftpFileTarget.REMOTE.value,
                    "path": "/home/user/.ssh/authorized_keys",
                    "service_id": "sftp-1",
                },
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_replace_file_rejected_while_draining():
    dispatcher, _runtime = _dispatcher()
    dispatcher.begin_shutdown()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "sftp.replace_file",
                {
                    "target": SftpFileTarget.REMOTE.value,
                    "path": "/home/user/.ssh/authorized_keys",
                    "content": "x",
                    "expected_revision": "rev-1",
                    "service_id": "sftp-1",
                },
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
