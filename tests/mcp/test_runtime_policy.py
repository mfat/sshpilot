"""Headless tests for the runtime MCP policy and handle.

These tests deliberately avoid importing the ``mcp`` SDK so they run in the
minimal environment (no optional ``mcp`` dependency). They assert the
READ / OPERATE / MUTATE policy boundary and the RuntimeHandle's wire mapping
against a fake daemon client.
"""

import pytest

from sshpilot.mcp.runtime.policy import PermissionLevel, PolicyError, RuntimePolicy
from sshpilot.mcp.runtime.server import RuntimeHandle, RuntimeToolError


class FakeClient:
    """Minimal stand-in for the daemon client surface (a subset of it)."""

    def __init__(self) -> None:
        self.calls = []

    def get_capabilities(self):
        self.calls.append("get_capabilities")
        return {"capabilities": ["x"]}

    def list_transfers(self):
        self.calls.append("list_transfers")
        return {"transfers": []}

    def open_session(self, request):
        self.calls.append(("open_session", request))
        return {"session_id": "s1"}

    def sftp_remove(self, request):
        self.calls.append(("sftp_remove", request))
        return None


def test_policy_defaults_read_broadly():
    policy = RuntimePolicy()
    assert policy.allows(PermissionLevel.READ)
    assert not policy.allows(PermissionLevel.OPERATE)
    assert not policy.allows(PermissionLevel.MUTATE)


def test_policy_requires_explicit_operate():
    policy = RuntimePolicy(allow_read=True, allow_operate=False)
    with pytest.raises(PolicyError):
        policy.require(PermissionLevel.OPERATE)


def test_policy_mutate_allows_sublevels():
    policy = RuntimePolicy(allow_read=True, allow_operate=True, allow_mutate=True)
    assert policy.allows(PermissionLevel.READ)
    assert policy.allows(PermissionLevel.OPERATE)
    assert policy.allows(PermissionLevel.MUTATE)


def test_policy_mutate_requires_operate():  # ordered levels
    policy = RuntimePolicy(allow_mutate=True, allow_operate=False)
    assert not policy.allows(PermissionLevel.MUTATE)


def test_policy_from_environment():
    policy = RuntimePolicy.from_environment(
        {"SSHPILOT_MCP_READ": "1", "SSHPILOT_MCP_OPERATE": "true", "SSHPILOT_MCP_MUTATE": "yes"}
    )
    assert policy.allows(PermissionLevel.MUTATE)
    disabled = RuntimePolicy.from_environment({"SSHPILOT_MCP_OPERATE": "0"})
    assert not disabled.allows(PermissionLevel.OPERATE)


def test_policy_content_opt_in_off_by_default_and_from_env():
    assert not RuntimePolicy().allows_content
    policy = RuntimePolicy.from_environment({"SSHPILOT_MCP_CONTENT": "1"})
    assert policy.allows_content
    disabled = RuntimePolicy.from_environment({})
    assert not disabled.allows_content


def test_policy_summary_reports_content():
    snapshot = RuntimePolicy().summary()
    assert snapshot["content"] is False
    snapshot = RuntimePolicy(allow_content=True).summary()
    assert snapshot["content"] is True


def test_handle_redacts_sensitive_dto_fields_by_default():
    from dataclasses import dataclass, field

    client = FakeClient()

    @dataclass(frozen=True)
    class _Sensitive:
        name: str
        content: str = field(repr=False)

    client.get_capabilities = (
        lambda: _Sensitive(name="file.txt", content="PRIVATE-KEY-MATERIAL")
    )
    handle = RuntimeHandle(client, RuntimePolicy(allow_read=True))
    result = handle._run("get_capabilities")
    assert result == {"name": "file.txt", "content": "<redacted>"}


def test_handle_content_opt_in_restores_dto_fields():
    from dataclasses import dataclass, field

    client = FakeClient()

    @dataclass(frozen=True)
    class _Sensitive:
        name: str
        content: str = field(repr=False)

    client.get_capabilities = (
        lambda: _Sensitive(name="file.txt", content="PRIVATE-KEY-MATERIAL")
    )
    handle = RuntimeHandle(client, RuntimePolicy(allow_read=True, allow_content=True))
    result = handle._run("get_capabilities")
    assert result == {"name": "file.txt", "content": "PRIVATE-KEY-MATERIAL"}


def test_handle_read_runs_through_client():
    client = FakeClient()
    handle = RuntimeHandle(client, RuntimePolicy(allow_read=True))
    result = handle.capabilities()
    assert result == {"capabilities": ["x"]}
    assert client.calls == ["get_capabilities"]


def test_handle_operate_denied_without_opt_in():
    client = FakeClient()
    handle = RuntimeHandle(client, RuntimePolicy())
    with pytest.raises(RuntimeToolError):
        handle.open_session("c1")
    assert client.calls == []


def test_handle_mutate_requires_confirm():
    client = FakeClient()
    handle = RuntimeHandle(
        client,
        RuntimePolicy(allow_read=True, allow_operate=True, allow_mutate=True),
    )
    with pytest.raises(RuntimeToolError):
        handle.sftp_remove("srv1", "/tmp/x")
    assert client.calls == []


def test_handle_mutate_denied_without_opt_in_even_with_confirm():
    client = FakeClient()
    handle = RuntimeHandle(client, RuntimePolicy())
    with pytest.raises(RuntimeToolError):
        handle.sftp_remove("srv1", "/tmp/x", confirm=True)
    assert client.calls == []


def test_handle_mutate_with_confirm_runs():
    client = FakeClient()
    handle = RuntimeHandle(
        client,
        RuntimePolicy(allow_read=True, allow_operate=True, allow_mutate=True),
    )
    result = handle.sftp_remove("srv1", "/tmp/x", confirm=True)
    assert result == {"removed": "/tmp/x"}
    assert client.calls[0][0] == "sftp_remove"


def test_handle_unknown_method_is_a_runtime_tool_error():
    client = FakeClient()
    handle = RuntimeHandle(client, RuntimePolicy(allow_read=True))
    with pytest.raises(RuntimeToolError):
        handle._run("no_such_method")


# ---- capability-driven tool visibility ------------------------------------


def test_tool_client_method_map_covers_all_tools():
    """Every advertised runtime tool dispatches through DaemonClient (D002)."""
    from sshpilot.api.daemon_client import DaemonClient

    from sshpilot.mcp.runtime.server import TOOL_CLIENT_METHOD, RuntimeHandle

    client = FakeClient()
    handle = RuntimeHandle(client, RuntimePolicy())
    daemon_methods = {
        name for name in dir(DaemonClient) if not name.startswith("_")
    }
    for tool_name, method_name in TOOL_CLIENT_METHOD.items():
        assert callable(getattr(handle, tool_name)), (
            f"tool {tool_name} has no RuntimeHandle method"
        )
        assert method_name in daemon_methods, (
            f"tool {tool_name} maps to non-client method {method_name}"
        )


def test_unsupported_capability_tools_are_removed():
    """Tools whose daemon capability is absent disappear from the surface."""
    from sshpilot.api.capabilities import Capabilities, Capability
    from sshpilot.api.models.common import (
        ClientInfo,
        CompatibilityResult,
        CoreInfo,
    )

    from sshpilot.mcp.runtime.server import TOOL_CLIENT_METHOD, _remove_unsupported_tools

    class RecordingServer:
        def __init__(self):
            self.removed = []

        def remove_tool(self, name):
            self.removed.append(name)

    caps = Capabilities(
        protocol_version="1.0",
        api_implementation_version="0.1",
        client=ClientInfo(name="x", version="1", client_id="c"),
        core=CoreInfo(name="x", version="1", implementation="daemon"),
        supported=frozenset({Capability.CONNECTIONS_READ}),
        compatibility=CompatibilityResult(
            compatible=True, protocol_version="1.0", message=""
        ),
    )

    server = RecordingServer()
    _remove_unsupported_tools(server, None, caps.supported)

    # Tools that need a missing capability must be removed.
    assert "open_sftp" in server.removed
    assert "list_sftp_services" in server.removed
    assert "open_session" in server.removed
    # Tools whose capability is present stay.
    assert "list_connections" not in server.removed
    # The map must be complete for every tool with a declared capability.
    from sshpilot.api.method_capabilities import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES

    declared = {
        tool for tool, method in TOOL_CLIENT_METHOD.items()
        if method in UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
    }
    assert declared, "expected at least some capability-gated runtime tools"


def test_unsupported_tools_filter_when_capabilities_unknown():
    """With no capability snapshot, every tool stays visible (call-time gate)."""
    from sshpilot.mcp.runtime.server import _remove_unsupported_tools

    class RecordingServer:
        def __init__(self):
            self.removed = []

        def remove_tool(self, name):
            self.removed.append(name)

    server = RecordingServer()
    _remove_unsupported_tools(server, None, None)
    assert server.removed == []