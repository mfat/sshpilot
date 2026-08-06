from __future__ import annotations

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import (
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
)
from sshpilot.api.transport.codec import (
    sftp_read_file_request_from_wire,
    sftp_read_file_request_to_wire,
    sftp_read_file_result_from_wire,
    sftp_read_file_result_to_wire,
    sftp_replace_file_request_from_wire,
    sftp_replace_file_request_to_wire,
    sftp_replace_file_result_from_wire,
    sftp_replace_file_result_to_wire,
)


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
