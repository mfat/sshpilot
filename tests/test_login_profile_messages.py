"""Frontend presentation of login profile previews, summaries and errors."""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.login_profiles import (
    LOGIN_PROFILE_ERROR_KEYWORD_DETAIL,
    LOGIN_PROFILE_ERROR_REASON_DETAIL,
    LoginProfileAssignmentPreview,
    LoginProfileErrorReason,
    LoginProfileField,
    LoginProfileFieldChange,
    LoginProfileSettings,
    LoginProfileSummary,
)
from sshpilot.gtk import login_profile_messages as messages
from sshpilot.gtk.login_profile_controller import (
    KeyPassphraseStorageError,
    LoginProfileSecretError,
)
from sshpilot.gtk.login_profile_messages import (
    field_label,
    format_changes_inline,
    format_field_value,
    format_login_profile_error,
    format_preview,
    profile_summary_line,
)


def _summary(**settings):
    return LoginProfileSummary(
        id="lp-aaaaaaaaaaaaaaaa",
        settings=LoginProfileSettings(name="Deploy", **settings),
        revision=1,
    )


def _rejected(reason, message="English daemon text", **extra):
    details = {LOGIN_PROFILE_ERROR_REASON_DETAIL: reason.value, **extra}
    return SshPilotError(ErrorCode.VALIDATION_FAILED, message, details=details)


def test_every_field_has_a_label():
    for field in LoginProfileField:
        assert field_label(field)
    assert field_label(LoginProfileField.IDENTITY_AGENT) == "IdentityAgent"


def test_raw_values_are_worded_by_the_frontend():
    assert format_field_value(LoginProfileField.AUTH_METHOD, "1") == "Password"
    assert format_field_value(LoginProfileField.KEY_SELECT_MODE, "2") == (
        "Specific keys and agent"
    )
    assert format_field_value(LoginProfileField.FORWARD_AGENT, "true") == "Yes"
    assert format_field_value(LoginProfileField.ADD_KEYS_TO_AGENT, "ask") == "Ask"
    assert format_field_value(LoginProfileField.IDENTITY_FILES, "/k/a\n/k/b") == "/k/a, /k/b"
    assert format_field_value(LoginProfileField.USERNAME, "") == "—"
    # An unexpected raw value is shown verbatim rather than dropped.
    assert format_field_value(LoginProfileField.AUTH_METHOD, "7") == "7"


def test_preview_text():
    text = format_preview((
        LoginProfileAssignmentPreview("web1", "Deploy", (
            LoginProfileFieldChange(LoginProfileField.USERNAME, "alice", "deploy"),
            LoginProfileFieldChange(LoginProfileField.IDENTITY_FILES, "", "/k/a\n/k/b"),
        )),
        LoginProfileAssignmentPreview("web2", "Deploy", ()),
    ))
    assert text == "web1:\n  Username: alice → deploy\n  Private Keys: — → /k/a, /k/b"
    assert format_changes_inline((
        LoginProfileFieldChange(LoginProfileField.AUTH_METHOD, "0", "1"),
        LoginProfileFieldChange(LoginProfileField.PUBKEY_AUTH_NO, "false", "true"),
    )) == (
        "Authentication: Key-based → Password; "
        "Disable public key authentication: No → Yes"
    )


def test_summary_line():
    assert profile_summary_line(_summary(username="deploy")) == "deploy · automatic keys"
    assert profile_summary_line(_summary(username="root", auth_method=1)) == "root · password"
    assert profile_summary_line(
        _summary(key_select_mode=1, identity_files=("~/.ssh/id_a", "/k/id_b"))
    ) == "id_a, id_b"


def test_the_presenter_translates_through_gettext(monkeypatch):
    monkeypatch.setattr(messages, "_", lambda text: f"<{text}>")
    assert profile_summary_line(_summary(auth_method=1)) == "<password>"
    assert format_login_profile_error(
        _rejected(LoginProfileErrorReason.NAME_EXISTS)
    ) == "<A login profile with this name already exists.>"


@pytest.mark.parametrize("reason", [
    r for r in LoginProfileErrorReason
    if r not in (LoginProfileErrorReason.INVALID_VALUE, LoginProfileErrorReason.EXTRA_MANAGED_OPTION)
])
def test_every_reason_has_its_own_message(reason):
    text = format_login_profile_error(_rejected(reason))
    assert text and "English daemon text" not in text


def test_managed_option_names_the_directive():
    text = format_login_profile_error(_rejected(
        LoginProfileErrorReason.EXTRA_MANAGED_OPTION,
        **{LOGIN_PROFILE_ERROR_KEYWORD_DETAIL: "identityfile"},
    ))
    assert text == "identityfile has its own setting and cannot be used in Extra SSH options."


def test_unexplained_failures_keep_the_diagnostic_opaque():
    invalid = format_login_profile_error(
        _rejected(LoginProfileErrorReason.INVALID_VALUE, "name must be a single line")
    )
    assert invalid == "The login profile operation failed.\n\nname must be a single line"
    # Unknown reason from a newer daemon: fall back to the generic message.
    newer = SshPilotError(
        ErrorCode.VALIDATION_FAILED, "detail",
        details={LOGIN_PROFILE_ERROR_REASON_DETAIL: "something_new"},
    )
    assert format_login_profile_error(newer) == "The login profile operation failed.\n\ndetail"


def test_error_codes_without_a_reason():
    stale = SshPilotError(ErrorCode.STALE_EDITOR, "modified since it was last read")
    assert format_login_profile_error(stale) == (
        "The login profile was changed elsewhere. Reopen it and try again."
    )
    offline = SshPilotError(ErrorCode.DAEMON_UNAVAILABLE, "not connected")
    assert format_login_profile_error(offline) == "Daemon connection unavailable."


def test_secret_failures_after_saving():
    inner = SshPilotError(ErrorCode.SECRET_STORAGE_FAILED, "backend said no")
    text = format_login_profile_error(LoginProfileSecretError(_summary(), inner))
    assert text == (
        "The profile was saved, but a password or passphrase could not be stored."
        "\n\nThe password could not be stored in secure storage."
    )
    passphrase = LoginProfileSecretError(_summary(), KeyPassphraseStorageError("x"))
    assert format_login_profile_error(passphrase).endswith(
        "\n\nThe key passphrase could not be stored."
    )
