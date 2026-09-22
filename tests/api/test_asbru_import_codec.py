"""Strict Ásbrú preview/result wire contract."""

from copy import deepcopy

import pytest

from sshpilot.api.models.connections import (
    AsbruImportMessage,
    AsbruImportMessageCode as Code,
    AsbruImportPreview,
    AsbruImportResult,
)
from sshpilot.api.transport.codec import (
    asbru_import_preview_from_wire,
    asbru_import_preview_to_wire,
    asbru_import_result_from_wire,
    asbru_import_result_to_wire,
)


def test_preview_messages_keep_exact_codes_parameters_and_diagnostics():
    preview = AsbruImportPreview(
        ok=False,
        source="/tmp/export.yml",
        warnings=(AsbruImportMessage(Code.SKIPPED_NON_SSH, {"name": "desk", "method": "VNC"}),),
        errors=(AsbruImportMessage(Code.YAML_INVALID, diagnostic="raw YAML marker"),),
    )
    wire = asbru_import_preview_to_wire(preview)
    assert wire["warnings"] == [{
        "code": "skipped_non_ssh",
        "parameters": {"name": "desk", "method": "VNC"},
        "diagnostic": "",
    }]
    assert wire["errors"][0]["diagnostic"] == "raw YAML marker"
    decoded = asbru_import_preview_from_wire(wire)
    assert decoded == preview


def test_result_messages_keep_summary_and_partial_failure_separate():
    result = AsbruImportResult(
        ok=False,
        source="export.yml",
        connections_added=("host",),
        message=AsbruImportMessage(Code.PARTIAL_FAILURES),
        partial_failures=(AsbruImportMessage(
            Code.ASSIGN_FAILED, {"nickname": "host"}, "opaque backend detail"
        ),),
        warnings=(AsbruImportMessage(Code.GROUPS_ONLY),),
    )
    wire = asbru_import_result_to_wire(result)
    assert wire["message"]["code"] == "partial_failures"
    assert wire["partial_failures"][0]["parameters"] == {"nickname": "host"}
    assert asbru_import_result_from_wire(wire) == result


@pytest.mark.parametrize("mutation", [
    lambda wire: wire["errors"].append("final English text"),
    lambda wire: wire["errors"][0].update(code="unknown_code"),
    lambda wire: wire["errors"][0].update(parameters={"unexpected": "value"}),
    lambda wire: wire["errors"][0].update(diagnostic=42),
    lambda wire: wire["errors"][0].update(extra="field"),
])
def test_unknown_or_malformed_preview_message_is_rejected(mutation):
    wire = asbru_import_preview_to_wire(AsbruImportPreview(
        ok=False, source="export.yml", errors=(AsbruImportMessage(Code.EXPORT_EMPTY),)
    ))
    wire = deepcopy(wire)
    mutation(wire)
    with pytest.raises((TypeError, ValueError)):
        asbru_import_preview_from_wire(wire)


def test_known_reason_rejects_wrong_parameters():
    with pytest.raises(ValueError):
        AsbruImportMessage(Code.SKIPPED_MISSING_HOST, {"other": "x"})


def test_known_reason_in_wrong_field_is_rejected():
    with pytest.raises(ValueError):
        AsbruImportPreview(
            ok=False, source="export.yml",
            warnings=(AsbruImportMessage(Code.IMPORTED),),
        )
    with pytest.raises(ValueError):
        AsbruImportResult(
            ok=False, source="export.yml",
            message=AsbruImportMessage(Code.SKIPPED_NON_SSH, {"name": "x", "method": "VNC"}),
        )
