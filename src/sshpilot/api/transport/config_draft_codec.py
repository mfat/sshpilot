"""Wire codec for SSH configuration draft evaluation.

Strict in both directions: unknown or missing fields are rejected, and the
models validate every value.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models.connections import (
    ConnectionEditEvaluation,
    ConnectionEditRefusal,
    EvaluateConnectionEditRequest,
    EvaluateSshConfigDraftRequest,
    SshConfigOwnLine,
    SshConfigDraftError,
    SshConfigDraftEvaluation,
    SshConfigDraftInclude,
    SshConfigDraftLine,
    SshConfigDraftLineState,
    SshConfigDraftSource,
    SshConfigDraftValue,
)
from .codec import _strict_fields

_REQUEST_FIELDS = {"text", "host", "user", "port", "allowed_exec"}
_EVALUATION_FIELDS = {
    "values", "defaults", "matched_blocks", "lines", "sources", "includes",
    "forced", "errors", "pending_exec", "system_config", "isolated",
}


def evaluate_ssh_config_draft_request_to_wire(
    request: EvaluateSshConfigDraftRequest,
) -> Dict[str, Any]:
    if type(request) is not EvaluateSshConfigDraftRequest:
        raise TypeError("an SSH config draft request is required")
    return {
        "text": request.text,
        "host": request.host,
        "user": request.user,
        "port": request.port,
        "allowed_exec": list(request.allowed_exec),
        **({"file": request.file} if request.file else {}),
    }


def evaluate_ssh_config_draft_request_from_wire(
    value: Any,
) -> EvaluateSshConfigDraftRequest:
    data = _strict_fields(
        value, required=_REQUEST_FIELDS, optional={"file"},
        context="SSH config draft request",
    )
    return EvaluateSshConfigDraftRequest(
        file=data.get("file", ""),
        text=data["text"],
        host=data["host"],
        user=data["user"],
        port=data["port"],
        allowed_exec=tuple(_list(data["allowed_exec"], "allowed exec")),
    )


def _values_to_wire(items) -> List[Dict[str, Any]]:
    return [{"key": item.key, "value": item.value} for item in items]


def _list(value: Any, name: str) -> list:
    if type(value) is not list:
        raise ValueError(f"SSH config draft {name} must be a list")
    return value


def _values_from_wire(value: Any, name: str):
    return tuple(
        SshConfigDraftValue(**_strict_fields(
            item, required={"key", "value"}, context=f"SSH config draft {name}"
        ))
        for item in _list(value, name)
    )


def ssh_config_draft_evaluation_to_wire(
    result: SshConfigDraftEvaluation,
) -> Dict[str, Any]:
    if type(result) is not SshConfigDraftEvaluation:
        raise TypeError("an SSH config draft evaluation is required")
    return {
        "values": _values_to_wire(result.values),
        "defaults": list(result.defaults),
        "matched_blocks": list(result.matched_blocks),
        "lines": [
            {
                "line": item.line,
                "state": item.state.value,
                "decided_file": item.decided_file,
                "decided_line": item.decided_line,
                "decided_block": item.decided_block,
            }
            for item in result.lines
        ],
        "sources": [
            {"key": item.key, "file": item.file, "line": item.line, "block": item.block}
            for item in result.sources
        ],
        "includes": [
            {"line": item.line, "files": list(item.files)} for item in result.includes
        ],
        "forced": _values_to_wire(result.forced),
        "errors": [
            {"file": item.file, "line": item.line, "diagnostic": item.diagnostic}
            for item in result.errors
        ],
        "pending_exec": list(result.pending_exec),
        "system_config": result.system_config,
        "isolated": result.isolated,
    }


def ssh_config_draft_evaluation_from_wire(value: Any) -> SshConfigDraftEvaluation:
    data = _strict_fields(
        value, required=_EVALUATION_FIELDS, context="SSH config draft evaluation"
    )
    lines = []
    for item in _list(data["lines"], "lines"):
        fields = _strict_fields(
            item,
            required={"line", "state", "decided_file", "decided_line", "decided_block"},
            context="SSH config draft line",
        )
        fields["state"] = SshConfigDraftLineState(fields["state"])
        lines.append(SshConfigDraftLine(**fields))
    sources = tuple(
        SshConfigDraftSource(**_strict_fields(
            item, required={"key", "file", "line", "block"},
            context="SSH config draft source",
        ))
        for item in _list(data["sources"], "sources")
    )
    includes = []
    for item in _list(data["includes"], "includes"):
        fields = _strict_fields(
            item, required={"line", "files"}, context="SSH config draft include"
        )
        includes.append(SshConfigDraftInclude(
            line=fields["line"], files=tuple(_list(fields["files"], "include files"))
        ))
    errors = tuple(
        SshConfigDraftError(**_strict_fields(
            item, required={"file", "line", "diagnostic"},
            context="SSH config draft error",
        ))
        for item in _list(data["errors"], "errors")
    )
    return SshConfigDraftEvaluation(
        values=_values_from_wire(data["values"], "values"),
        defaults=tuple(_list(data["defaults"], "defaults")),
        matched_blocks=tuple(_list(data["matched_blocks"], "matched blocks")),
        lines=tuple(lines),
        sources=sources,
        includes=tuple(includes),
        forced=_values_from_wire(data["forced"], "forced"),
        errors=errors,
        pending_exec=tuple(_list(data["pending_exec"], "pending exec")),
        system_config=data["system_config"],
        isolated=data["isolated"],
    )


# -- a connection edit, as saving it would write it --------------------------

_EDIT_REQUEST_FIELDS = {"connection_id", "update", "create", "without_own"}
_EDIT_EVALUATION_FIELDS = {
    "host", "file", "text", "own_lines", "evaluation", "without_own", "refusal",
}


def evaluate_connection_edit_request_to_wire(
    request: EvaluateConnectionEditRequest,
) -> Dict[str, Any]:
    from .codec import (
        create_connection_request_to_wire,
        update_connection_request_to_wire,
    )

    if type(request) is not EvaluateConnectionEditRequest:
        raise TypeError("a connection edit evaluation request is required")
    return {
        "connection_id": request.connection_id,
        "update": (
            None if request.update is None
            else update_connection_request_to_wire(request.update)
        ),
        "create": (
            None if request.create is None
            else create_connection_request_to_wire(request.create)
        ),
        "without_own": list(request.without_own),
    }


def evaluate_connection_edit_request_from_wire(
    value: Any,
) -> EvaluateConnectionEditRequest:
    from .codec import (
        create_connection_request_from_wire,
        update_connection_request_from_wire,
    )

    data = _strict_fields(
        value, required=_EDIT_REQUEST_FIELDS, context="connection edit request",
    )
    connection_id = data["connection_id"]
    if connection_id is not None and type(connection_id) is not str:
        raise ValueError("connection edit connection_id must be a string")
    return EvaluateConnectionEditRequest(
        connection_id=connection_id,
        update=(
            None if data["update"] is None
            else update_connection_request_from_wire(data["update"])
        ),
        create=(
            None if data["create"] is None
            else create_connection_request_from_wire(data["create"])
        ),
        without_own=tuple(_list(data["without_own"], "without own")),
    )


def connection_edit_evaluation_to_wire(
    result: ConnectionEditEvaluation,
) -> Dict[str, Any]:
    if type(result) is not ConnectionEditEvaluation:
        raise TypeError("a connection edit evaluation is required")
    return {
        "host": result.host,
        "file": result.file,
        "text": result.text,
        "own_lines": [
            {"line": item.line, "key": item.key, "value": item.value}
            for item in result.own_lines
        ],
        "evaluation": (
            None if result.evaluation is None
            else ssh_config_draft_evaluation_to_wire(result.evaluation)
        ),
        "without_own": (
            None if result.without_own is None
            else ssh_config_draft_evaluation_to_wire(result.without_own)
        ),
        "refusal": None if result.refusal is None else result.refusal.value,
    }


def connection_edit_evaluation_from_wire(value: Any) -> ConnectionEditEvaluation:
    data = _strict_fields(
        value, required=_EDIT_EVALUATION_FIELDS, context="connection edit evaluation",
    )
    own_lines = tuple(
        SshConfigOwnLine(**_strict_fields(
            item, required={"line", "key", "value"}, context="connection edit own line",
        ))
        for item in _list(data["own_lines"], "own lines")
    )
    return ConnectionEditEvaluation(
        host=data["host"],
        file=data["file"],
        text=data["text"],
        own_lines=own_lines,
        evaluation=(
            None if data["evaluation"] is None
            else ssh_config_draft_evaluation_from_wire(data["evaluation"])
        ),
        without_own=(
            None if data["without_own"] is None
            else ssh_config_draft_evaluation_from_wire(data["without_own"])
        ),
        refusal=(
            None if data["refusal"] is None
            else ConnectionEditRefusal(data["refusal"])
        ),
    )
