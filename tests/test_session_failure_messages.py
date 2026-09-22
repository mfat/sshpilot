"""Frontend presentation of structured daemon session failures."""

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.sessions import SessionFailure, SessionFailureCode
from sshpilot.gtk import session_failure_messages as messages


def test_every_session_failure_code_has_a_frontend_template():
    assert set(messages._SESSION_FAILURE_TEMPLATES) == set(SessionFailureCode)


@pytest.mark.parametrize("code", tuple(SessionFailureCode))
def test_session_failure_reason_is_translated_at_render_time(monkeypatch, code):
    seen = []

    def translate(msgid):
        seen.append(msgid)
        return f"translated:{msgid}"

    monkeypatch.setattr(messages, "_", translate)
    parameters = {"status": 255} if code is SessionFailureCode.SSH_EXITED else {}
    failure = SessionFailure(code, ErrorCode.SESSION_STARTUP_FAILED, parameters)

    rendered = messages.format_session_failure(failure)

    assert rendered.startswith("translated:")
    assert seen == [messages._SESSION_FAILURE_TEMPLATES[code]]
    if parameters:
        assert rendered.endswith("255")
        assert "{status}" in seen[0]


def test_diagnostic_is_separate_and_never_passed_to_gettext(monkeypatch):
    seen = []
    monkeypatch.setattr(
        messages,
        "_",
        lambda msgid: (seen.append(msgid), f"translated:{msgid}")[1],
    )
    failure = SessionFailure(
        SessionFailureCode.SSH_DIAGNOSTIC,
        ErrorCode.SESSION_STARTUP_FAILED,
        diagnostic="opaque OpenSSH: Permission denied (publickey)",
    )

    assert messages.format_session_failure(failure) == "translated:The SSH session failed."
    assert messages.format_session_failure(
        failure, include_diagnostic=True
    ) == (
        "translated:The SSH session failed.\n\n"
        "opaque OpenSSH: Permission denied (publickey)"
    )
    assert seen == ["The SSH session failed."] * 2


@pytest.mark.parametrize(
    "code,error_code,parameters,diagnostic",
    [
        (SessionFailureCode.SSH_EXITED, ErrorCode.SESSION_STARTUP_FAILED, {}, ""),
        (SessionFailureCode.SSH_EXITED, ErrorCode.SESSION_STARTUP_FAILED, {"status": True}, ""),
        (SessionFailureCode.SSH_EXITED, ErrorCode.SESSION_STARTUP_FAILED, {"status": 0}, ""),
        (SessionFailureCode.START_FAILED, ErrorCode.SESSION_STARTUP_FAILED, {"status": 255}, ""),
        ("unknown", ErrorCode.SESSION_STARTUP_FAILED, {}, ""),
        (SessionFailureCode.START_FAILED, "unknown", {}, ""),
        (SessionFailureCode.START_FAILED, ErrorCode.SESSION_STARTUP_FAILED, {}, "bad\x00detail"),
    ],
)
def test_session_failure_rejects_invalid_fields(code, error_code, parameters, diagnostic):
    with pytest.raises((TypeError, ValueError)):
        SessionFailure(code, error_code, parameters, diagnostic)
