import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import SftpFailure, SftpFailureCode
from sshpilot.gtk import sftp_failure_messages as messages


def test_frontend_mapping_is_exhaustive():
    assert set(messages._SFTP_FAILURE_TEMPLATES) == set(SftpFailureCode)


@pytest.mark.parametrize(
    ("code", "parameter_name", "parameter_value", "msgid"),
    (
        (
            SftpFailureCode.REMOTE_FILE_BLOCKS_DIRECTORY,
            "remote_dir",
            "/remote/{literal}/é",
            "A remote file is in the way of a directory: {remote_dir}",
        ),
        (
            SftpFailureCode.REMOTE_DIRECTORY_CREATION_FAILED,
            "remote_dir",
            "/remote/new dir",
            "The remote directory could not be created: {remote_dir}",
        ),
        (
            SftpFailureCode.LOCAL_DESTINATION_EXISTS,
            "path",
            "/local/{literal}/é",
            "The local destination already exists: {path}",
        ),
        (
            SftpFailureCode.REMOTE_DESTINATION_EXISTS,
            "path",
            "/remote/existing file",
            "The remote destination already exists: {path}",
        ),
    ),
)
def test_gettext_runs_before_exact_parameter_formatting(
    monkeypatch, code, parameter_name, parameter_value, msgid
):
    calls = []

    def translate(value):
        calls.append(value)
        return f"traduit: {value}"

    monkeypatch.setattr(messages, "_", translate)
    failure = SftpFailure(
        code,
        ErrorCode.TRANSFER_CONFLICT,
        parameters={parameter_name: parameter_value},
    )

    assert messages.format_sftp_failure(failure) == f"traduit: {msgid}".format(
        **{parameter_name: parameter_value}
    )
    assert calls == [msgid]


def test_diagnostic_is_appended_without_gettext(monkeypatch):
    calls = []

    def translate(value):
        calls.append(value)
        return "La commande SFTP a échoué"

    monkeypatch.setattr(messages, "_", translate)
    diagnostic = "vendor status=4: storage pool Q is read-only"
    failure = SftpFailure(
        SftpFailureCode.COMMAND_FAILED,
        ErrorCode.SFTP_COMMAND_FAILED,
        diagnostic=diagnostic,
    )

    assert messages.format_sftp_failure(failure) == (
        f"La commande SFTP a échoué\n\n{diagnostic}"
    )
    assert calls == ["The SFTP command failed"]


def test_presenter_rejects_a_non_sftp_failure():
    with pytest.raises(ValueError, match="invalid SFTP failure"):
        messages.format_sftp_failure(object())
