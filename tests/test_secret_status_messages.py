import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.secrets import (
    BitwardenStatus,
    RbwStatus,
    SecretMessageCode,
    SecretOperationResult,
    SecretOperationState,
    SecretUnlockResult,
    UnlockResultKind,
)
from sshpilot.gtk import secret_status_messages as messages


def test_frontend_translates_before_formatting_and_preserves_dynamic_value(monkeypatch):
    calls = []

    def translate(msgid):
        calls.append(msgid)
        return "Backend traduit : {backend}"

    monkeypatch.setattr(messages, "_", translate)
    result = SecretUnlockResult(
        kind=UnlockResultKind.BACKEND_UNAVAILABLE,
        backend="custom-vault",
        message_code=SecretMessageCode.SECRET_BACKEND_UNAVAILABLE,
        message_parameters={"backend": "custom-vault"},
    )

    assert messages.format_secret_message(result) == "Backend traduit : custom-vault"
    assert calls == ["Secret backend '{backend}' is unavailable"]


def test_frontend_keeps_external_diagnostic_separate_and_untranslated(monkeypatch):
    monkeypatch.setattr(messages, "_", lambda _msgid: "Échec de connexion")
    result = BitwardenStatus(
        logged_in=False,
        unlocked=False,
        needs_login=True,
        email="alice@example.com",
        server_url="",
        profile="",
        message_code=SecretMessageCode.BITWARDEN_SIGN_IN_FAILED,
        diagnostic="bw: invalid grant for alice@example.com",
    )

    assert messages.format_secret_message(result) == (
        "Échec de connexion\n\nbw: invalid grant for alice@example.com"
    )


def test_frontend_presents_bitwarden_unlock_failure_from_login():
    result = BitwardenStatus(
        logged_in=False,
        unlocked=False,
        needs_login=True,
        email="alice@example.com",
        server_url="",
        profile="",
        message_code=SecretMessageCode.BITWARDEN_UNLOCK_FAILED,
    )

    assert messages.format_secret_message(result) == "Bitwarden vault unlock failed"


def test_frontend_translates_structured_backend_unavailable_error(monkeypatch):
    monkeypatch.setattr(messages, "_", lambda _msgid: "{backend} est indisponible")
    error = SshPilotError(
        ErrorCode.SECRET_BACKEND_UNAVAILABLE,
        ErrorCode.SECRET_BACKEND_UNAVAILABLE.value,
        details={"backend": "bitwarden"},
    )

    assert messages.format_secret_error(error) == "Bitwarden est indisponible"


def test_frontend_preserves_unclassified_external_error():
    error = RuntimeError("bw exited with status 1")

    assert messages.format_secret_error(error) == "bw exited with status 1"


def test_success_has_no_message_and_does_not_translate(monkeypatch):
    monkeypatch.setattr(
        messages,
        "_",
        lambda _msgid: pytest.fail("success must not request a translation"),
    )
    result = SecretOperationResult(
        state=SecretOperationState.SUCCESS,
        backend="keepassxc",
    )

    assert messages.format_secret_message(result) == ""


def test_non_displayed_result_code_has_no_artificial_frontend_mapping():
    result = RbwStatus(
        installed=True,
        configured=True,
        unlocked=False,
        email="alice@example.com",
        base_url="",
        message_code=SecretMessageCode.RBW_UNLOCK_FAILED,
    )

    with pytest.raises(ValueError, match="no frontend presentation"):
        messages.format_secret_message(result)
