import pytest

from sshpilot.api.models.common import ConnectionId, RequestId, SessionId
from sshpilot.api.models.operations import (
    FileEntryKind,
    ForwardKind,
    ForwardState,
    ListDirectoryRequest,
    PluginArgument,
    PluginOperationRequest,
    PluginOperationResult,
    PortForwardSummary,
    SftpEntry,
)


def test_sftp_models_are_transport_neutral():
    request = ListDirectoryRequest(
        connection_id=ConnectionId("test"),
        path="/var/log",
    )
    entry = SftpEntry(
        name="messages",
        path="/var/log/messages",
        kind=FileEntryKind.FILE,
        size=42,
    )

    assert request.path == "/var/log"
    assert entry.size == 42


def test_port_forward_model_validates_ports():
    with pytest.raises(ValueError):
        PortForwardSummary(
            id="forward-1",
            session_id=SessionId("session-1"),
            kind=ForwardKind.LOCAL,
            state=ForwardState.STARTING,
            bind_host="127.0.0.1",
            bind_port=0,
        )


def test_plugin_operation_has_explicit_public_arguments():
    request = PluginOperationRequest(
        request_id=RequestId("request-1"),
        plugin_id="example.plugin",
        operation="status",
        arguments=(PluginArgument(name="verbose", value="true"),),
    )

    assert request.arguments[0].value == "true"
    assert "true" not in repr(request.arguments[0])


def test_plugin_result_values_are_excluded_from_repr():
    result = PluginOperationResult(
        request_id=RequestId("request-1"),
        plugin_id="example.plugin",
        values=(("token", "potentially-sensitive-result"),),
    )

    assert "potentially-sensitive-result" not in repr(result)
