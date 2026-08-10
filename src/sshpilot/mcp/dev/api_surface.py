"""API surface inspection and drift detection for the dev MCP.

Pure static analysis over ``docs/api/generated/schema.json``,
``src/sshpilot/api``, and ``src/sshpilot/daemon``. Nothing is imported or
executed, so the checks work on a half-broken checkout and the dev server
stays read-only and repository-confined.
"""

from __future__ import annotations

import ast
import json
from typing import Dict, Iterable, List, Optional, Tuple

from .._scope import RepoScope, ScopeError

SCHEMA_RELPATH = "docs/api/generated/schema.json"
CLIENT_RELPATH = "src/sshpilot/api/client.py"
DAEMON_CLIENT_RELPATH = "src/sshpilot/api/daemon_client.py"
DISPATCH_RELPATH = "src/sshpilot/daemon/dispatch.py"
UNSUPPORTED_RELPATH = "src/sshpilot/api/method_capabilities.py"
IMPLEMENTED_RELPATH = "src/sshpilot/core/connection_application_service.py"
CAPABILITIES_RELPATH = "src/sshpilot/api/capabilities.py"

#: First-N examples reported for each drift category, bounding responses.
DRIFT_EXAMPLE_LIMIT = 20


class ApiNotFoundError(ValueError):
    """Raised when a requested API method cannot be located."""


def _read(scope: RepoScope, relpath: str) -> str:
    path = scope.resolve(relpath)
    if not path.is_file():
        raise ScopeError(f"not a file inside repository: {relpath!r}")
    return path.read_text(encoding="utf-8")


def load_schema(scope: RepoScope) -> dict:
    """Load and validate the generated transport-neutral API schema."""
    text = _read(scope, SCHEMA_RELPATH)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid schema JSON at {SCHEMA_RELPATH}: {error}") from error
    for key in (
        "protocol_version",
        "api_implementation_version",
        "client_method_contract",
        "daemon_method_contract",
        "models",
        "identifiers",
        "enums",
    ):
        if key not in data:
            raise ValueError(f"schema at {SCHEMA_RELPATH} is missing section {key!r}")
    return data


def _parse_rel(scope: RepoScope, relpath: str) -> ast.Module:
    return ast.parse(_read(scope, relpath), filename=relpath)


# --------------------------------------------------------------------------
# AST extraction helpers
# --------------------------------------------------------------------------
def _annotation_string(node) -> str:
    """Render an annotation AST node (unquoted, evaluated form) to a string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_annotation_string(node.value)}.{node.attr}"
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        if isinstance(node.value, str):
            return node.value
        return repr(node.value)
    if isinstance(node, ast.Subscript):
        value = _annotation_string(node.value)
        if isinstance(node.slice, ast.Tuple):
            args = ", ".join(_annotation_string(item) for item in node.slice.elts)
        else:
            args = _annotation_string(node.slice)
        return f"{value}[{args}]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_annotation_string(node.left)} | {_annotation_string(node.right)}"
    if isinstance(node, ast.Tuple):
        return ", ".join(_annotation_string(item) for item in node.elts)
    return "?"


def _method_defs(tree: ast.Module, class_name: Optional[str] = None) -> Iterable[Tuple[ast.FunctionDef, str]]:
    """Yield (FunctionDef, enclosing-class-or-"")."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and (class_name is None or node.name == class_name):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield sub, node.name


def client_signatures(scope: RepoScope) -> Dict[str, dict]:
    """Map client method name → parameter/return annotation strings."""
    tree = _parse_rel(scope, CLIENT_RELPATH)
    result: Dict[str, dict] = {}
    for node, _class in _method_defs(tree, "SshPilotClient"):
        parameters = []
        positional = node.args.args
        required_count = len(positional) - len(node.args.defaults)
        for index, arg in enumerate(positional):
            if arg.arg == "self":
                continue
            annotation = _annotation_string(arg.annotation) if arg.annotation else "untyped"
            parameters.append(
                {
                    "name": arg.arg,
                    "type": annotation,
                    "required": index < required_count,
                }
            )
        result[node.name] = {
            "parameters": parameters,
            "return": (
                _annotation_string(node.returns) if node.returns else "untyped"
            ),
        }
    return result


def client_to_wire(scope: RepoScope) -> Dict[str, List[str]]:
    """Map DaemonClient method name → the wire method strings it requests."""
    tree = _parse_rel(scope, DAEMON_CLIENT_RELPATH)
    result: Dict[str, List[str]] = {}
    for node, _class in _method_defs(tree, "DaemonClient"):
        wire = []
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            func = call.func
            is_request = (
                isinstance(func, ast.Attribute)
                and func.attr == "_request"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            )
            is_subscribe = (
                isinstance(func, ast.Attribute)
                and func.attr == "_subscribe"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            )
            if not (is_request or is_subscribe):
                continue
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                wire.append(first.value)
        if wire:
            result[node.name] = sorted(set(wire))
    return result


def _capability_value_map(scope: RepoScope) -> Dict[str, str]:
    """Map ``Capability.MEMBER`` name → its enum value (from capabilities.py)."""
    tree = _parse_rel(scope, CAPABILITIES_RELPATH)
    mapped: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Capability":
            continue
        for sub in node.body:
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and isinstance(sub.value, ast.Constant)
                and isinstance(sub.value.value, str)
            ):
                mapped[sub.targets[0].id] = sub.value.value
    return mapped


def _module_dict(scope: RepoScope, relpath: str, name: str) -> Dict[str, Optional[str]]:
    """Extract a ``NAME = {"key": Capability.X | None, ...}`` literal dict."""
    tree = _parse_rel(scope, relpath)
    capability_values = _capability_value_map(scope)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            continue
        result: Dict[str, Optional[str]] = {}
        for key, item in zip(value.keys, value.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name):
                result[key.value] = capability_values.get(item.attr, item.attr)
            elif isinstance(item, ast.Constant) and item.value is None:
                result[key.value] = None
        return result
    return {}


def wire_handlers(scope: RepoScope) -> Dict[str, str]:
    """Map wire method → daemon dispatch handler name (from HANDLERS)."""
    tree = _parse_rel(scope, DISPATCH_RELPATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        else:
            continue
        if not (isinstance(target, ast.Attribute) and target.attr == "HANDLERS"):
            continue
        if not isinstance(value, ast.Dict):
            continue
        result = {}
        for key, item in zip(value.keys, value.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name):
                result[key.value] = item.attr
        return result
    return {}


def wire_capabilities(scope: RepoScope) -> Dict[str, Optional[str]]:
    """Map wire method → capability name (from DAEMON_METHOD_CAPABILITIES)."""
    return _module_dict(scope, DISPATCH_RELPATH, "DAEMON_METHOD_CAPABILITIES")


def _client_capability_sources(scope: RepoScope) -> Dict[str, Dict[str, Optional[str]]]:
    return {
        "implemented": _module_dict(scope, IMPLEMENTED_RELPATH, "IMPLEMENTED_CLIENT_METHOD_CAPABILITIES"),
        "daemon": _module_dict(
            scope, DAEMON_CLIENT_RELPATH, "DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES"
        ),
        "unsupported": _module_dict(
            scope, UNSUPPORTED_RELPATH, "UNSUPPORTED_CLIENT_METHOD_CAPABILITIES"
        ),
    }


def reconcile_client_contract(scope: RepoScope) -> Dict[str, dict]:
    """Mirror the generator's client contract reconciliation from source AST."""
    sources = _client_capability_sources(scope)
    implemented = sources["implemented"]
    daemon = sources["daemon"]
    unsupported = sources["unsupported"]
    result: Dict[str, dict] = {}
    for name, capability in implemented.items():
        result[name] = {"status": "implemented", "capability": capability}
    for name, capability in unsupported.items():
        daemon_cap = daemon.get(name)
        result[name] = {
            "status": "daemon-only" if daemon_cap is not None else "schema-only",
            "capability": daemon_cap if daemon_cap is not None else capability,
        }
    return result


def list_methods(scope: RepoScope, surface: str = "all") -> dict:
    """List client and/or daemon wire methods with status and capability."""
    if surface not in {"all", "client", "daemon"}:
        raise ValueError("surface must be 'all', 'client', or 'daemon'")
    schema = load_schema(scope)
    client_contract = schema["client_method_contract"]
    daemon_contract = schema["daemon_method_contract"]
    client = sorted(
        ({"name": name, **entry} for name, entry in client_contract.items()),
        key=lambda item: item["name"],
    )
    daemon = sorted(
        (
            {"name": name, "capability": entry.get("capability")}
            for name, entry in daemon_contract.items()
        ),
        key=lambda item: item["name"],
    )
    if surface == "client":
        return {"surface": "client", "total": len(client), "methods": client}
    if surface == "daemon":
        return {"surface": "daemon", "total": len(daemon), "methods": daemon}
    return {
        "surface": "all",
        "client_total": len(client),
        "daemon_total": len(daemon),
        "client_methods": client,
        "daemon_methods": daemon,
    }


def inspect_method(scope: RepoScope, name: str) -> dict:
    """Trace a client method or a daemon wire method end to end."""
    schema = load_schema(scope)
    client_contract = schema["client_method_contract"]
    daemon_contract = schema["daemon_method_contract"]
    client_to_wire_map = client_to_wire(scope)
    handlers = wire_handlers(scope)
    daemon_caps = wire_capabilities(scope)
    signatures = client_signatures(scope)

    if name in client_contract:
        contract = client_contract[name]
        wire = client_to_wire_map.get(name, [])
        wire_details = []
        for method in wire:
            wire_details.append(
                {
                    "method": method,
                    "capability": daemon_caps.get(method),
                    "handler": handlers.get(method),
                }
            )
        return {
            "role": "client",
            "name": name,
            "status": contract["status"],
            "requirement": contract.get("capability"),
            "signature": signatures.get(name, {}),
            "wire_methods": wire_details,
        }
    if name in daemon_contract:
        reversed_map: Dict[str, List[str]] = {}
        for client_method, wire in client_to_wire_map.items():
            for method in wire:
                reversed_map.setdefault(method, []).append(client_method)
        return {
            "role": "daemon",
            "name": name,
            "requirement": daemon_contract[name].get("capability"),
            "handler": handlers.get(name),
            "capability_source": daemon_caps.get(name),
            "called_by": sorted(reversed_map.get(name, [])),
        }
    raise ApiNotFoundError(f"unknown API method: {name!r}")


def check_drift(scope: RepoScope) -> dict:
    """Compare the generated schema against the source registrations."""
    schema = load_schema(scope)
    schema_client = schema["client_method_contract"]
    schema_daemon = schema["daemon_method_contract"]

    try:
        source_client = reconcile_client_contract(scope)
    except ValueError as error:
        return {
            "clean": False,
            "schema_path": SCHEMA_RELPATH,
            "total_findings": 1,
            "categories": [
                {
                    "category": "client-contract",
                    "count": 1,
                    "examples": [str(error)],
                }
            ],
        }

    source_handlers = wire_handlers(scope)
    source_daemon_caps = wire_capabilities(scope)

    findings: List[dict] = []
    client_in_schema = set(schema_client)
    client_in_source = set(source_client)
    for name in sorted(client_in_schema - client_in_source):
        findings.append({"category": "client-contract", "detail": f"schema-only client method not in source: {name}"})
    for name in sorted(client_in_source - client_in_schema):
        findings.append({"category": "client-contract", "detail": f"source client method missing from schema: {name}"})
    for name in sorted(client_in_source & client_in_schema):
        expected = source_client[name]
        actual = schema_client[name]
        if expected["status"] != actual.get("status"):
            findings.append(
                {"category": "client-contract", "detail": f"{name}: status {actual.get('status')!r} != source {expected['status']!r}"}
            )
        if actual.get("capability") != expected["capability"]:
            findings.append(
                {"category": "client-contract", "detail": f"{name}: capability {actual.get('capability')!r} != source {expected['capability']!r}"}
            )

    daemon_in_schema = set(schema_daemon)
    daemon_in_source = set(source_handlers)
    for name in sorted(daemon_in_schema - daemon_in_source):
        findings.append({"category": "daemon-handlers", "detail": f"schema wire method has no dispatch handler: {name}"})
    for name in sorted(daemon_in_source - daemon_in_schema):
        findings.append({"category": "daemon-handlers", "detail": f"dispatch handler not in schema: {name}"})
    for name in sorted(daemon_in_schema & daemon_in_source):
        if schema_daemon[name].get("capability") != source_daemon_caps.get(name):
            findings.append(
                {"category": "daemon-capability", "detail": f"{name}: schema capability {schema_daemon[name].get('capability')!r} != source {source_daemon_caps.get(name)!r}"}
            )

    categories = {}
    for finding in findings:
        categories.setdefault(finding["category"], []).append(finding["detail"])
    categorized = [
        {
            "category": category,
            "count": len(items),
            "examples": items[:DRIFT_EXAMPLE_LIMIT],
        }
        for category, items in sorted(categories.items())
    ]
    return {
        "clean": not findings,
        "schema_path": SCHEMA_RELPATH,
        "total_findings": len(findings),
        "categories": categorized,
    }


def inspect_api(scope: RepoScope) -> dict:
    """Return the overall API snapshot from the generated schema."""
    schema = load_schema(scope)
    client_contract = schema["client_method_contract"]
    daemon_contract = schema["daemon_method_contract"]
    statuses: Dict[str, int] = {}
    for entry in client_contract.values():
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    return {
        "format": schema.get("format"),
        "protocol_version": schema["protocol_version"],
        "api_implementation_version": schema["api_implementation_version"],
        "client_methods": len(client_contract),
        "daemon_methods": len(daemon_contract),
        "models": len(schema["models"]),
        "identifiers": len(schema["identifiers"]),
        "enums": len(schema["enums"]),
        "client_statuses": statuses,
    }