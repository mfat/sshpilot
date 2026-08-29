import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import IdentityFailure, IdentityFailureCode
from sshpilot.gtk import identity_failure_messages as messages


def test_frontend_mapping_is_exhaustive():
    assert set(messages._IDENTITY_FAILURE_TEMPLATES) == set(IdentityFailureCode)


def test_frontend_translates_without_using_operation_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        messages,
        "_",
        lambda msgid: calls.append(msgid) or "Authentification refusée",
    )
    failure = IdentityFailure(
        IdentityFailureCode.AUTHENTICATION_FAILED,
        ErrorCode.REMOTE_COMMAND_FAILED,
    )

    assert messages.format_identity_failure(failure) == "Authentification refusée"
    assert calls == ["Authentication failed while installing the public key"]


def test_external_diagnostic_is_appended_without_gettext(monkeypatch):
    calls = []
    monkeypatch.setattr(
        messages,
        "_",
        lambda msgid: calls.append(msgid) or "Installation impossible",
    )
    diagnostic = "Permission denied (publickey)"
    failure = IdentityFailure(
        IdentityFailureCode.AUTHENTICATION_FAILED,
        ErrorCode.REMOTE_COMMAND_FAILED,
        diagnostic=diagnostic,
    )

    assert messages.format_identity_failure(failure) == (
        f"Installation impossible\n\n{diagnostic}"
    )
    assert calls == ["Authentication failed while installing the public key"]


def test_presenter_rejects_non_identity_failure():
    with pytest.raises(ValueError, match="invalid identity failure"):
        messages.format_identity_failure(object())
