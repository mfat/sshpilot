"""Transfer start failures use codes; diagnostics never select a translation."""

import gettext
import io
import json
import subprocess

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.gtk import sftp_error_messages, sftp_failure_messages
from sshpilot.gtk import transfer_error_messages as messages


@pytest.mark.parametrize("capability", ["transfers.batch", "transfers.upload", "transfers.download"])
def test_capability_uses_details_and_translates_before_format(monkeypatch, capability):
    calls = []
    monkeypatch.setattr(
        messages, "_", lambda msgid: calls.append(msgid) or "Capacité indisponible : {capability}",
    )
    error = SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY, "misleading daemon wording: transfers.other",
        details={"capability": capability},
    )
    assert messages.format_transfer_start_error(error) == f"Capacité indisponible : {capability}"
    assert calls == ["The daemon does not support {capability}"]


@pytest.mark.parametrize("details", [{}, {"capability": None}, {"capability": 7}, {"capability": ""}])
def test_missing_or_invalid_capability_never_parses_message(monkeypatch, details):
    calls = []
    monkeypatch.setattr(messages, "_", lambda msgid: calls.append(msgid) or "Démarrage impossible")
    error = SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY, "The daemon does not support transfers.batch", details=details,
    )
    assert messages.format_transfer_start_error(error) == "Démarrage impossible"
    assert calls == ["The transfer could not be started"]


@pytest.mark.parametrize("code,msgid", [
    (ErrorCode.INVALID_REQUEST, "The transfer could not be started"),
    (ErrorCode.INTERNAL_ERROR, "The transfer could not be started"),
    (ErrorCode.SERVER_BUSY, "The daemon transfer command queue is full"),
    (ErrorCode.DAEMON_SHUTTING_DOWN, "The daemon is shutting down"),
    (ErrorCode.DAEMON_UNAVAILABLE, "The daemon transfer service is unavailable."),
    (ErrorCode.TRANSPORT_CLOSED, "The daemon connection was closed."),
    (ErrorCode.TRANSPORT_TIMEOUT, "The daemon transfer request timed out."),
    (ErrorCode.PROTOCOL_ERROR, "The transfer could not be started"),
    (ErrorCode.API_VERSION_MISMATCH, "The daemon API version does not match this application."),
    (ErrorCode.CONNECTION_NOT_FOUND, "The selected connection no longer exists in the daemon."),
    (ErrorCode.MUTATION_AMBIGUOUS, "The transfer may have started. Refresh before trying again."),
])
def test_start_rpc_errors_use_stable_codes(monkeypatch, code, msgid):
    calls = []
    monkeypatch.setattr(messages, "_", lambda value: calls.append(value) or f"translated:{value}")
    assert messages.format_transfer_start_error(SshPilotError(code, "unstable wording")) == f"translated:{msgid}"
    assert calls == [msgid]


@pytest.mark.parametrize("error", [
    RuntimeError("external {capability}\nraw diagnostic\n"),
    SshPilotError(ErrorCode.TRANSFER_IO_FAILED, "opaque filesystem diagnostic"),
])
def test_unknown_diagnostics_stay_opaque(monkeypatch, error):
    monkeypatch.setattr(messages, "_", lambda _: pytest.fail("diagnostic passed to gettext"))
    monkeypatch.setattr(sftp_error_messages, "_", lambda _: pytest.fail("diagnostic passed to gettext"))
    assert messages.format_transfer_start_error(error) == str(error)


def test_sftp_presenter_keeps_specific_diagnostic_unchanged(monkeypatch):
    diagnostic = "remote {name}\npermission denied\n"
    monkeypatch.setattr(messages, "_", lambda _: pytest.fail("transfer template used"))
    monkeypatch.setattr(sftp_error_messages, "_", lambda _: "Accès refusé")
    error = SshPilotError(ErrorCode.REMOTE_PERMISSION_DENIED, "ignored", details={
        "server_message": diagnostic, "server_message_is_specific": True,
    })
    assert messages.format_transfer_start_error(error) == "Accès refusé\n\n" + diagnostic


def test_structured_sftp_failure_takes_priority(monkeypatch):
    monkeypatch.setattr(messages, "_", lambda _: pytest.fail("generic template used"))
    monkeypatch.setattr(sftp_failure_messages, "_", lambda _: "Le service SFTP n’est pas prêt")
    error = SshPilotError(ErrorCode.INVALID_REQUEST, "ignored", details={"sftp_failure_code": "service_not_ready"})
    assert messages.format_transfer_start_error(error) == "Le service SFTP n’est pas prêt"


def test_real_french_catalogue_formats_capability(monkeypatch):
    header = "Content-Type: text/plain; charset=UTF-8\nLanguage: fr\n"
    po = '\n'.join([
        'msgid ""', 'msgstr ' + json.dumps(header), '', '#, python-brace-format',
        'msgid "The daemon does not support {capability}"',
        'msgstr "Le daemon ne prend pas en charge {capability}"', '',
    ])
    compiled = subprocess.run(
        ["msgfmt", "--check-format", "-o", "-", "-"],
        input=po.encode(), capture_output=True, check=True,
    )
    catalogue = gettext.GNUTranslations(io.BytesIO(compiled.stdout))
    monkeypatch.setattr(messages, "_", catalogue.gettext)
    error = SshPilotError(ErrorCode.UNSUPPORTED_CAPABILITY, "ignored", details={"capability": "transfers.batch"})
    assert messages.format_transfer_start_error(error) == "Le daemon ne prend pas en charge transfers.batch"
