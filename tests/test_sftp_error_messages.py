import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.gtk import sftp_error_messages as messages


@pytest.mark.parametrize(
    ("code", "msgid"),
    (
        (ErrorCode.REMOTE_PATH_NOT_FOUND, "The path was not found"),
        (ErrorCode.REMOTE_PERMISSION_DENIED, "Permission denied"),
        (ErrorCode.SFTP_PROTOCOL_LOST, "The SFTP connection was lost"),
        (
            ErrorCode.REMOTE_UNSUPPORTED_OPERATION,
            "The server does not support this operation",
        ),
        (ErrorCode.SFTP_COMMAND_FAILED, "The SFTP command failed"),
        (ErrorCode.SFTP_PROTOCOL_ERROR, "The SFTP command failed"),
    ),
)
def test_direct_sftp_error_code_selects_frontend_msgid(monkeypatch, code, msgid):
    calls = []

    def translate(value):
        calls.append(value)
        return f"translated:{value}"

    monkeypatch.setattr(messages, "_", translate)
    error = SshPilotError(code, code.value)

    assert messages.format_direct_sftp_error(error) == f"translated:{msgid}"
    assert calls == [msgid]


def test_specific_server_diagnostic_is_appended_without_gettext(monkeypatch):
    calls = []

    def translate(value):
        calls.append(value)
        return "La commande SFTP a échoué"

    monkeypatch.setattr(messages, "_", translate)
    diagnostic = "remote appliance rejected inode 42"
    error = SshPilotError(
        ErrorCode.SFTP_COMMAND_FAILED,
        ErrorCode.SFTP_COMMAND_FAILED.value,
        details={
            "sftp_status": 4,
            "server_message": diagnostic,
            "server_message_is_specific": True,
        },
    )

    assert messages.format_direct_sftp_error(error) == (
        f"La commande SFTP a échoué\n\n{diagnostic}"
    )
    assert calls == ["The SFTP command failed"]


def test_generic_server_status_is_not_displayed_as_diagnostic(monkeypatch):
    monkeypatch.setattr(messages, "_", lambda _value: "Échec SFTP")
    error = SshPilotError(
        ErrorCode.SFTP_COMMAND_FAILED,
        ErrorCode.SFTP_COMMAND_FAILED.value,
        details={"sftp_status": 4, "server_message": "Failure"},
    )

    assert messages.format_direct_sftp_error(error) == "Échec SFTP"


def test_service_failure_family_is_not_presented_by_direct_formatter(monkeypatch):
    monkeypatch.setattr(
        messages,
        "_",
        lambda _value: pytest.fail("ServiceFailure text must stay out of this pass"),
    )
    error = SshPilotError(
        ErrorCode.SFTP_SERVICE_NOT_READY,
        "The SFTP session could not be established",
    )

    assert messages.format_direct_sftp_error(error) == (
        "The SFTP session could not be established"
    )


def test_specific_diagnostic_metadata_is_strict():
    error = SshPilotError(
        ErrorCode.SFTP_COMMAND_FAILED,
        ErrorCode.SFTP_COMMAND_FAILED.value,
        details={"server_message_is_specific": "yes"},
    )

    with pytest.raises(ValueError, match="classification is invalid"):
        messages.format_direct_sftp_error(error)
