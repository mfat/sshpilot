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
        (ErrorCode.SFTP_SERVICE_NOT_FOUND, "The SFTP service was not found"),
        (ErrorCode.SFTP_SERVICE_NOT_READY, "The SFTP service is not ready"),
        (ErrorCode.SERVICE_OWNER_REQUIRED, "Only the originating client may mutate this SFTP service"),
        (ErrorCode.INVALID_REQUEST, "The SFTP command failed"),
        (ErrorCode.INTERNAL_ERROR, "The SFTP command failed"),
        (ErrorCode.PROTOCOL_ERROR, "The SFTP command failed"),
        (ErrorCode.TRANSPORT_CLOSED, "The daemon connection was closed."),
        (ErrorCode.TRANSPORT_TIMEOUT, "The daemon request timed out."),
        (ErrorCode.DAEMON_SHUTTING_DOWN, "The daemon is shutting down"),
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


def test_direct_not_ready_code_ignores_unstable_message(monkeypatch):
    monkeypatch.setattr(
        messages,
        "_",
        lambda value: "Le service SFTP n’est pas prêt" if value == "The SFTP service is not ready" else value,
    )
    error = SshPilotError(
        ErrorCode.SFTP_SERVICE_NOT_READY,
        "The SFTP session could not be established",
    )

    assert messages.format_direct_sftp_error(error) == "Le service SFTP n’est pas prêt"


@pytest.mark.parametrize("capability", ["sftp.file_ops", "sftp.read"])
def test_sftp_capability_failure_translates_from_details(monkeypatch, capability):
    calls = []
    monkeypatch.setattr(messages, "_", lambda value: calls.append(value) or "Capacité indisponible : {capability}")
    error = SshPilotError(ErrorCode.UNSUPPORTED_CAPABILITY, "ignored", details={"capability": capability})
    assert messages.format_direct_sftp_error(error) == f"Capacité indisponible : {capability}"
    assert calls == ["The daemon does not support {capability}"]


def test_specific_diagnostic_metadata_is_strict():
    error = SshPilotError(
        ErrorCode.SFTP_COMMAND_FAILED,
        ErrorCode.SFTP_COMMAND_FAILED.value,
        details={"server_message_is_specific": "yes"},
    )

    with pytest.raises(ValueError, match="classification is invalid"):
        messages.format_direct_sftp_error(error)


def test_direct_error_naming_a_failure_code_uses_its_frontend_msgid(monkeypatch):
    from sshpilot.gtk import sftp_failure_messages

    calls = []
    monkeypatch.setattr(messages, "_", lambda value: pytest.fail("generic template used"))
    monkeypatch.setattr(
        sftp_failure_messages,
        "_",
        lambda value: calls.append(value) or f"translated:{value}",
    )
    error = SshPilotError(
        ErrorCode.VALIDATION_FAILED,
        "Refusing to recursively delete the root or home directory",
        details={"sftp_failure_code": "recursive_delete_protected_path"},
    )

    assert messages.has_structured_sftp_failure(error)
    assert messages.format_direct_sftp_error(error) == (
        "translated:The root folder and your home folder cannot be deleted"
    )
    assert calls == ["The root folder and your home folder cannot be deleted"]


@pytest.mark.parametrize(
    "value",
    (
        "reason_from_a_newer_daemon",  # unknown to this frontend
        "remote_destination_exists",  # needs a parameter details cannot carry
        7,
    ),
)
def test_unusable_failure_code_detail_falls_back_to_generic_presentation(value):
    error = SshPilotError(
        ErrorCode.VALIDATION_FAILED,
        "daemon message",
        details={"sftp_failure_code": value},
    )

    assert not messages.has_structured_sftp_failure(error)
    assert messages.format_direct_sftp_error(error) == "daemon message"
