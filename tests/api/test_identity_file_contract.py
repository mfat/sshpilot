from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.operations import (
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
)
from sshpilot.api.transport.codec import (
    error_to_wire,
    sftp_read_file_request_from_wire,
    sftp_read_file_request_to_wire,
    sftp_read_file_result_from_wire,
    sftp_read_file_result_to_wire,
    sftp_replace_file_request_from_wire,
    sftp_replace_file_request_to_wire,
    sftp_replace_file_result_from_wire,
    sftp_replace_file_result_to_wire,
)
from sshpilot.api.transport.envelopes import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
)

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "scripts" / "generate_api_artifacts.py"

FILE_CONTENT_SENTINEL = "sftp-file-content-token-9f3a2b"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_api_artifacts", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_file_request_round_trips():
    request = SftpReadFileRequest(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "~/.ssh/authorized_keys",
    )
    assert sftp_read_file_request_from_wire(
        sftp_read_file_request_to_wire(request)
    ) == request


def test_file_replacement_request_round_trips():
    request = SftpReplaceFileRequest(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "~/.ssh/authorized_keys",
        "ssh-ed25519 AAAA comment\n",
        "absent",
        backup=True,
    )
    assert sftp_replace_file_request_from_wire(
        sftp_replace_file_request_to_wire(request)
    ) == request


def test_file_results_round_trip():
    read = SftpReadFileResult(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "/home/test/.ssh/authorized_keys",
        "comment\n",
        True,
        "revision",
        8,
        0o600,
    )
    replaced = SftpReplaceFileResult(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "/home/test/.ssh/authorized_keys",
        "new-revision",
        8,
        "/home/test/.ssh/authorized_keys.bak-1",
    )
    assert sftp_read_file_result_from_wire(sftp_read_file_result_to_wire(read)) == read
    assert sftp_replace_file_result_from_wire(
        sftp_replace_file_result_to_wire(replaced)
    ) == replaced


def test_file_codecs_reject_unknown_fields():
    request = {
        "target": "local_authorized_keys",
        "path": "~/.ssh/authorized_keys",
        "service_id": None,
        "unexpected": "sentinel",
    }
    with pytest.raises(ValueError):
        sftp_read_file_request_from_wire(request)


def test_local_file_target_rejects_arbitrary_path_before_transport():
    with pytest.raises(ValueError):
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "/etc/passwd")
    with pytest.raises(ValueError):
        SftpReplaceFileRequest(
            SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
            "/etc/passwd",
            "secret",
            "absent",
        )


def test_error_code_is_stable():
    assert ErrorCode.FILE_REVISION_CONFLICT.value == "file_revision_conflict"


def _read_result_with_sentinel() -> SftpReadFileResult:
    return SftpReadFileResult(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "/home/test/.ssh/authorized_keys",
        FILE_CONTENT_SENTINEL,
        True,
        "revision",
        len(FILE_CONTENT_SENTINEL),
        0o600,
    )


def _replace_request_with_sentinel() -> SftpReplaceFileRequest:
    return SftpReplaceFileRequest(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "~/.ssh/authorized_keys",
        FILE_CONTENT_SENTINEL,
        "absent",
        backup=True,
    )


def test_file_content_is_excluded_from_model_reprs():
    assert FILE_CONTENT_SENTINEL not in repr(_read_result_with_sentinel())
    assert FILE_CONTENT_SENTINEL not in repr(_replace_request_with_sentinel())


def test_file_content_is_excluded_from_model_diagnostics():
    assert FILE_CONTENT_SENTINEL not in str(_read_result_with_sentinel())
    assert FILE_CONTENT_SENTINEL not in str(_replace_request_with_sentinel())
    assert FILE_CONTENT_SENTINEL not in repr(SftpReplaceFileResult(
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        "/home/test/.ssh/authorized_keys",
        "new-revision",
        len(FILE_CONTENT_SENTINEL),
        "/home/test/.ssh/authorized_keys.bak-1",
    ))


def test_file_content_is_excluded_from_transport_envelope_reprs():
    request_envelope = RequestEnvelope(
        "1.0",
        "request-1",
        "sftp.replace_file",
        sftp_replace_file_request_to_wire(_replace_request_with_sentinel()),
    )
    response_envelope = SuccessResponseEnvelope(
        "1.0",
        "request-2",
        sftp_read_file_result_to_wire(_read_result_with_sentinel()),
    )
    assert FILE_CONTENT_SENTINEL not in repr(request_envelope)
    assert FILE_CONTENT_SENTINEL not in repr(response_envelope)


def test_file_content_is_excluded_from_logged_transport_failures(caplog):
    import logging

    logger = logging.getLogger("sshpilot.test.transport")
    logger.exception("Deferred daemon method sftp.replace_file failed type=%s", "SshPilotError")
    logger.error("The SFTP command failed details=%r", {"service_id": "svc-1"})
    assert FILE_CONTENT_SENTINEL not in caplog.text


def test_file_content_is_excluded_from_error_text():
    error = SshPilotError(
        ErrorCode.FILE_REVISION_CONFLICT,
        "The remote file changed since it was read",
    )
    assert FILE_CONTENT_SENTINEL not in str(error)
    assert FILE_CONTENT_SENTINEL not in str(error.to_dict())

    wire_error = error_to_wire(error)
    assert FILE_CONTENT_SENTINEL not in repr(wire_error)

    envelope = ErrorResponseEnvelope("1.0", "request-1", wire_error)
    assert FILE_CONTENT_SENTINEL not in repr(envelope)


def test_file_content_is_excluded_from_codec_failure_text():
    with pytest.raises(ValueError) as raised:
        sftp_replace_file_request_from_wire(
            {
                "target": "local_authorized_keys",
                "path": "~/.ssh/authorized_keys",
                "content": FILE_CONTENT_SENTINEL,
                "expected_revision": "absent",
                "backup": True,
                "service_id": None,
                "unexpected": "field",
            }
        )
    assert FILE_CONTENT_SENTINEL not in str(raised.value)


def test_generated_safe_model_representations_omit_file_content():
    generator = _load_generator()
    schema = generator.build_schema()

    for model_name, field_name in (
        ("SftpReadFileResult", "content"),
        ("SftpReplaceFileRequest", "content"),
    ):
        fields = {
            field_schema["name"]: field_schema
            for field_schema in schema["models"][model_name]["fields"]
        }
        assert fields[field_name]["sensitive"] is True

    examples = generator._example_for_model
    assert examples(
        _replace_request_with_sentinel().__class__
    )["content"] == "<sensitive value omitted>"
    assert examples(_read_result_with_sentinel().__class__)[
        "content"
    ] == "<sensitive value omitted>"


def test_generated_artifacts_mark_file_content_sensitive():
    import json

    schema = json.loads(
        (ROOT / "docs" / "api" / "generated" / "schema.json").read_text(
            encoding="utf-8"
        )
    )
    for model_name in ("SftpReadFileResult", "SftpReplaceFileRequest"):
        content = next(
            field_schema
            for field_schema in schema["models"][model_name]["fields"]
            if field_schema["name"] == "content"
        )
        assert content["sensitive"] is True
    index = (ROOT / "docs" / "api" / "generated" / "model-index.md").read_text(
        encoding="utf-8"
    )
    assert "| `content` | `str` | Yes | — | Yes |" in index
