import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import ScpFailure, ScpFailureCode
from sshpilot.gtk import scp_failure_messages as messages


def test_frontend_mapping_is_exhaustive():
    assert set(messages._SCP_FAILURE_TEMPLATES) == set(ScpFailureCode)


def test_frontend_translates_without_using_transport_text(monkeypatch):
    calls = []
    monkeypatch.setattr(
        messages,
        "_",
        lambda msgid: calls.append(msgid) or "Le transfert SCP a échoué.",
    )
    failure = ScpFailure(
        ScpFailureCode.TRANSFER_FAILED,
        ErrorCode.TRANSFER_IO_FAILED,
    )

    assert messages.format_scp_failure(failure) == "Le transfert SCP a échoué."
    assert calls == ["The SCP transfer failed."]


def test_external_diagnostic_is_appended_without_gettext(monkeypatch):
    calls = []
    monkeypatch.setattr(
        messages,
        "_",
        lambda msgid: calls.append(msgid) or "Échec SCP",
    )
    diagnostic = "scp: remote vendor status=23"
    failure = ScpFailure(
        ScpFailureCode.TRANSFER_FAILED,
        ErrorCode.TRANSFER_IO_FAILED,
        diagnostic=diagnostic,
    )

    assert messages.format_scp_failure(failure) == f"Échec SCP\n\n{diagnostic}"
    assert calls == ["The SCP transfer failed."]


def test_presenter_rejects_non_scp_failure():
    with pytest.raises(ValueError, match="invalid SCP failure"):
        messages.format_scp_failure(object())
