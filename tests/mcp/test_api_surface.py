"""Unit and integration tests for dev-MCP API-surface intelligence."""

import json
from pathlib import Path

import pytest

from sshpilot.mcp._scope import RepoScope
from sshpilot.mcp.dev import api_surface


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def repo():
    return RepoScope(REPO_ROOT)


@pytest.fixture()
def schema():
    with (REPO_ROOT / "docs/api/generated/schema.json").open() as handle:
        return json.load(handle)


def test_load_schema_has_contracts(repo):
    schema = api_surface.load_schema(repo)
    assert schema["format"] == "sshpilot-api-schema-v1"
    assert schema["protocol_version"]
    assert schema["api_implementation_version"]
    assert schema["client_method_contract"]
    assert schema["daemon_method_contract"]
    assert schema["models"]


def test_inspect_api_counts(repo):
    result = api_surface.inspect_api(repo)
    assert result["protocol_version"] == "1.0"
    assert result["client_methods"] >= 100
    assert result["daemon_methods"] >= result["client_methods"]
    assert result["models"] >= 100
    assert set(result["client_statuses"]) <= {"implemented", "daemon-only", "schema-only"}


def test_list_methods_all_client_daemon(repo):
    all_result = api_surface.list_methods(repo)
    assert all_result["client_total"] >= 100
    assert all_result["daemon_total"] >= 100
    client = api_surface.list_methods(repo, surface="client")
    daemon = api_surface.list_methods(repo, surface="daemon")
    assert client["surface"] == "client"
    assert daemon["surface"] == "daemon"
    assert len(client["methods"]) == all_result["client_total"]
    assert len(daemon["methods"]) == all_result["daemon_total"]
    with pytest.raises(ValueError):
        api_surface.list_methods(repo, surface="bogus")


def test_client_signatures_parse(repo):
    signatures = api_surface.client_signatures(repo)
    params = signatures["open_session"]["parameters"]
    assert params == [
        {"name": "request", "type": "OpenSessionRequest", "required": True}
    ]
    assert signatures["open_session"]["return"] == "SessionSummary"
    # required/optional split is correct
    any_optional = any(
        param["required"] is False
        for params in signatures.values()
        for param in params["parameters"]
    )
    assert any_optional


def test_client_to_wire_maps_request(repo):
    mapping = api_surface.client_to_wire(repo)
    assert mapping["open_session"] == ["sessions.open"]
    assert mapping["list_connections"] == ["connections.list"]


def test_wire_handlers_from_annotated_dict(repo):
    handlers = api_surface.wire_handlers(repo)
    assert handlers["sessions.open"] == "_handle_open_session"
    assert handlers["connections.list"] == "_handle_list_connections"


def test_wire_capabilities_resolve_to_values(repo):
    capabilities = api_surface.wire_capabilities(repo)
    assert capabilities["sessions.open"] == "sessions.write"
    assert capabilities["connections.list"] == "connections.read"


def test_inspect_client_method_trace(repo):
    result = api_surface.inspect_method(repo, "open_session")
    assert result["role"] == "client"
    assert result["name"] == "open_session"
    assert result["requirement"] == "sessions.write"
    assert [w["method"] for w in result["wire_methods"]] == ["sessions.open"]
    assert result["wire_methods"][0]["handler"] == "_handle_open_session"


def test_inspect_daemon_method_trace(repo):
    result = api_surface.inspect_method(repo, "sessions.open")
    assert result["role"] == "daemon"
    assert result["handler"] == "_handle_open_session"
    assert result["requirement"] == "sessions.write"
    assert result["called_by"] == ["open_session"]


def test_inspect_unknown_method(repo):
    with pytest.raises(api_surface.ApiNotFoundError):
        api_surface.inspect_method(repo, "no.such.method")


def test_capability_value_map(repo):
    mapping = api_surface._capability_value_map(repo)
    assert mapping["SESSIONS_WRITE"] == "sessions.write"
    assert mapping["CONNECTIONS_READ"] == "connections.read"


def test_check_drift_clean_on_real_schema(repo):
    result = api_surface.check_drift(repo)
    assert result["clean"] is True
    assert result["total_findings"] == 0
    assert result["schema_path"] == api_surface.SCHEMA_RELPATH
    assert result["categories"] == []


def test_client_contract_reconciliation(repo):
    from sshpilot.mcp.dev.api_surface import reconcile_client_contract

    contract = reconcile_client_contract(repo)
    schema = api_surface.load_schema(repo)
    for name, entry in contract.items():
        assert schema["client_method_contract"][name]["status"] == entry["status"]
        assert (
            schema["client_method_contract"][name].get("capability") == entry["capability"]
        )