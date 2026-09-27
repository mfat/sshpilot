"""Frontend-owned presentation of login profiles, previews and errors.

The daemon sends stable identifiers only: ``LoginProfileField`` plus raw
values in assignment previews, and a ``LoginProfileErrorReason`` in rejected
request details. Everything a person reads is worded and translated here.
SSH directive names stay untranslated; unexpected daemon or backend detail is
appended as an opaque diagnostic.
"""

from __future__ import annotations

from gettext import gettext as _
from typing import Iterable, List

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.login_profiles import (
    LOGIN_PROFILE_ERROR_KEYWORD_DETAIL,
    LOGIN_PROFILE_ERROR_REASON_DETAIL,
    LoginProfileAssignmentPreview,
    LoginProfileErrorReason,
    LoginProfileField,
    LoginProfileFieldChange,
    LoginProfileSummary,
)
from ..i18n import N_
from .login_profile_controller import KeyPassphraseStorageError, LoginProfileSecretError


# SSH directive names are technical identifiers and stay as they are.
_FIELD_LABELS = {
    LoginProfileField.USERNAME: N_("Username"),
    LoginProfileField.AUTH_METHOD: N_("Authentication"),
    LoginProfileField.KEY_SELECT_MODE: N_("Key selection"),
    LoginProfileField.IDENTITY_FILES: N_("Private Keys"),
    LoginProfileField.CERTIFICATE_FILES: N_("Certificates"),
    LoginProfileField.ADD_KEYS_TO_AGENT: N_("Add keys to agent"),
    LoginProfileField.PKCS11_PROVIDER: N_("PKCS#11 provider"),
    LoginProfileField.SECURITY_KEY_PROVIDER: N_("FIDO security key provider"),
    LoginProfileField.PUBKEY_AUTH_NO: N_("Disable public key authentication"),
    LoginProfileField.FORWARD_AGENT: N_("Forward agent"),
    LoginProfileField.FORWARD_AGENT_TARGET: N_("Agent socket to forward"),
    LoginProfileField.EXTRA: N_("Extra SSH options"),
}
_UNTRANSLATED_FIELD_LABELS = {
    LoginProfileField.IDENTITY_AGENT: "IdentityAgent",
}

_AUTH_METHOD_VALUES = {"0": N_("Key-based"), "1": N_("Password")}
_KEY_SELECT_MODE_VALUES = {
    "0": N_("Automatic"),
    "1": N_("Specific keys only"),
    "2": N_("Specific keys and agent"),
}
_BOOL_VALUES = {"true": N_("Yes"), "false": N_("No")}
_ADD_KEYS_VALUES = {
    "yes": N_("Yes"),
    "no": N_("No"),
    "ask": N_("Ask"),
    "confirm": N_("Confirm"),
}
_VALUE_TABLES = {
    LoginProfileField.AUTH_METHOD: _AUTH_METHOD_VALUES,
    LoginProfileField.KEY_SELECT_MODE: _KEY_SELECT_MODE_VALUES,
    LoginProfileField.PUBKEY_AUTH_NO: _BOOL_VALUES,
    LoginProfileField.FORWARD_AGENT: _BOOL_VALUES,
    LoginProfileField.ADD_KEYS_TO_AGENT: _ADD_KEYS_VALUES,
}
_EMPTY_VALUE = "—"

_REASON_TEMPLATES = {
    LoginProfileErrorReason.PROFILE_NOT_FOUND: N_("The login profile no longer exists."),
    LoginProfileErrorReason.NAME_EXISTS: N_(
        "A login profile with this name already exists."
    ),
    LoginProfileErrorReason.NAME_EMPTY: N_("Enter a name for the login profile."),
    LoginProfileErrorReason.NAME_TOO_LONG: N_("The login profile name is too long."),
    LoginProfileErrorReason.USERNAME_WHITESPACE: N_(
        "The username must not contain spaces."
    ),
    LoginProfileErrorReason.EXTRA_FORBIDDEN_BLOCK: N_(
        "Extra SSH options cannot contain Host, Match or Include lines."
    ),
    LoginProfileErrorReason.EXTRA_MANAGED_OPTION: N_(
        "{keyword} has its own setting and cannot be used in Extra SSH options."
    ),
    LoginProfileErrorReason.NOT_SSH_CONNECTION: N_(
        "Login profiles apply to SSH connections."
    ),
    LoginProfileErrorReason.GROUP_NOT_FOUND: N_("The group no longer exists."),
    LoginProfileErrorReason.REPLACEMENT_INVALID: N_(
        "The replacement login profile is no longer available."
    ),
    LoginProfileErrorReason.BACKUP_INVALID: N_("The login profile backup is invalid."),
    LoginProfileErrorReason.PROFILES_UNAVAILABLE: N_(
        "Login profiles are unavailable: their file could not be read."
    ),
    LoginProfileErrorReason.SECRET_STORAGE_UNAVAILABLE: N_(
        "Secure storage is not available for login profile passwords."
    ),
}

_ERROR_CODE_TEMPLATES = {
    ErrorCode.STALE_EDITOR: N_(
        "The login profile was changed elsewhere. Reopen it and try again."
    ),
    ErrorCode.CONNECTION_NOT_FOUND: N_("The connection no longer exists."),
    ErrorCode.SECRET_STORAGE_FAILED: N_(
        "The password could not be stored in secure storage."
    ),
    ErrorCode.DAEMON_UNAVAILABLE: N_("Daemon connection unavailable."),
    ErrorCode.TRANSPORT_CLOSED: N_("Daemon connection unavailable."),
    ErrorCode.UNSUPPORTED_CAPABILITY: N_(
        "The SSH Pilot daemon does not support login profiles."
    ),
}


# ---------------------------------------------------------------------------
# Profiles and assignment previews
# ---------------------------------------------------------------------------

def field_label(field: LoginProfileField) -> str:
    """The label of one profile-owned setting."""
    if field in _UNTRANSLATED_FIELD_LABELS:
        return _UNTRANSLATED_FIELD_LABELS[field]
    return _(_FIELD_LABELS[field])


def format_field_value(field: LoginProfileField, raw: str) -> str:
    """One raw preview value as display text (lists joined on one line)."""
    if not raw:
        return _EMPTY_VALUE
    table = _VALUE_TABLES.get(field)
    if table is not None and raw in table:
        return _(table[raw])
    return ", ".join(line for line in raw.splitlines() if line) or _EMPTY_VALUE


def format_field_change(change: LoginProfileFieldChange) -> str:
    """``Label: before → after`` for one changed setting."""
    return _("{label}: {before} → {after}").format(
        label=field_label(change.field),
        before=format_field_value(change.field, change.before),
        after=format_field_value(change.field, change.after),
    )


def format_preview(previews: Iterable[LoginProfileAssignmentPreview]) -> str:
    """Plain-text diff for a confirmation dialog (empty when nothing changes)."""
    lines: List[str] = []
    for preview in previews:
        if not preview.changes:
            continue
        lines.append(f"{preview.connection_id}:")
        lines.extend(f"  {format_field_change(change)}" for change in preview.changes)
    return "\n".join(lines)


def format_changes_inline(changes: Iterable[LoginProfileFieldChange]) -> str:
    """The changes for one connection on a single line (row subtitles)."""
    return "; ".join(format_field_change(change) for change in changes)


def profile_summary_line(profile: LoginProfileSummary) -> str:
    """Short one-line description for rows and subtitles."""
    settings = profile.settings
    parts = []
    if settings.username:
        parts.append(settings.username)
    if settings.auth_method == 1:
        parts.append(_("password"))
    elif settings.identity_files:
        parts.append(", ".join(path.rsplit("/", 1)[-1] for path in settings.identity_files))
    else:
        parts.append(_("automatic keys"))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def format_login_profile_error(error: BaseException) -> str:
    """Translate a login profile failure; keep unexpected detail opaque."""
    if isinstance(error, LoginProfileSecretError):
        return (
            _("The profile was saved, but a password or passphrase could not be stored.")
            + "\n\n"
            + format_login_profile_error(error.error)
        )
    if isinstance(error, KeyPassphraseStorageError):
        return _("The key passphrase could not be stored.")
    if isinstance(error, SshPilotError):
        message = _reason_message(error)
        if message is not None:
            return message
        template = _ERROR_CODE_TEMPLATES.get(error.code)
        if template is not None:
            return _(template)
    message = _("The login profile operation failed.")
    diagnostic = str(getattr(error, "message", None) or error).strip()
    return f"{message}\n\n{diagnostic}" if diagnostic else message


def _reason_message(error: SshPilotError):
    details = error.details
    try:
        reason = LoginProfileErrorReason(details.get(LOGIN_PROFILE_ERROR_REASON_DETAIL))
    except ValueError:
        return None  # no reason, or one from a newer daemon
    template = _REASON_TEMPLATES.get(reason)
    if template is None:
        return None  # INVALID_VALUE: the generic message plus the diagnostic
    if reason is LoginProfileErrorReason.EXTRA_MANAGED_OPTION:
        keyword = details.get(LOGIN_PROFILE_ERROR_KEYWORD_DETAIL)
        if type(keyword) is not str or not keyword:
            return None
        return _(template).format(keyword=keyword)
    return _(template)
