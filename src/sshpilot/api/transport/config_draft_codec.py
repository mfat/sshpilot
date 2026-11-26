"""Wire codec for SSH configuration draft evaluation.

Strict in both directions: unknown or missing fields are rejected, and the
models validate every value.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models.connections import (
    EvaluateSshConfigDraftRequest,
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
