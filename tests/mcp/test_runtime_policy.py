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