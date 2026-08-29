"""Frontend-owned presentation of structured secret status messages."""

from __future__ import annotations

from gettext import gettext as _
from typing import Any, Mapping

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.secrets import SecretMessageCode
from ..i18n import N_


_SECRET_MESSAGE_TEMPLATES = {
    SecretMessageCode.SECRET_BACKEND_UNAVAILABLE: N_(
        "Secret backend '{backend}' is unavailable"
    ),
    SecretMessageCode.UNLOCK_CANCELLED: N_("Unlock cancelled"),
    SecretMessageCode.VAULT_UNLOCK_FAILED: N_("The vault could not be unlocked"),
    SecretMessageCode.BACKEND_UNAVAILABLE: N_("{backend} is unavailable"),
    SecretMessageCode.BITWARDEN_SERVER_CONFIGURATION_FAILED: N_(
        "Bitwarden server configuration failed"
    ),
    SecretMessageCode.BITWARDEN_LOGIN_CANCELLED: N_("Login cancelled"),
    SecretMessageCode.BITWARDEN_AUTHENTICATION_CHALLENGE_CANCELLED: N_(
        "Authentication challenge cancelled"
    ),
    SecretMessageCode.BITWARDEN_TWO_STEP_LOGIN_CANCELLED: N_(
        "Two-step login cancelled"
    ),
    SecretMessageCode.BITWARDEN_SIGN_IN_FAILED: N_("Sign-in failed."),
    SecretMessageCode.BITWARDEN_UNLOCK_FAILED: N_(
        "Bitwarden vault unlock failed"
    ),
    SecretMessageCode.RBW_SYNC_FAILED: N_("rbw sync failed"),
    SecretMessageCode.DATABASE_PATH_REQUIRED: N_("A database path is required"),
    SecretMessageCode.KEEPASS_DATABASE_CREATE_OR_UNLOCK_FAILED: N_(
        "The KeePass database could not be created or unlocked"
    ),
}

_SECRET_ERROR_TEMPLATES = {
    ErrorCode.SECRET_BACKEND_UNAVAILABLE: N_("{backend} is unavailable"),
}

_BACKEND_DISPLAY_NAMES = {
    "bitwarden": "Bitwarden",
    "keepassxc": "KeePassXC",
    "rbw": "rbw",
}


def _display_parameters(parameters: Mapping[str, str]) -> dict[str, str]:
    display = dict(parameters)
    if "backend" in display:
        backend = display["backend"]
        display["backend"] = _BACKEND_DISPLAY_NAMES.get(backend, backend)
    return display


def _render(template: str, parameters: Mapping[str, str], diagnostic: str) -> str:
    message = _(template).format(**_display_parameters(parameters))
    return f"{message}\n\n{diagnostic}" if diagnostic else message


def format_secret_message(result: Any) -> str:
    """Translate one structured result message and append its raw diagnostic."""

    code = getattr(result, "message_code", None)
    if code is None:
        return ""
    if not isinstance(code, SecretMessageCode):
        raise ValueError("secret message code is invalid")
    try:
        template = _SECRET_MESSAGE_TEMPLATES[code]
    except KeyError:
        raise ValueError("secret message code has no frontend presentation") from None
    parameters = getattr(result, "message_parameters", {})
    diagnostic = getattr(result, "diagnostic", "")
    if not isinstance(parameters, Mapping) or not isinstance(diagnostic, str):
        raise ValueError("secret message metadata is invalid")
    return _render(template, parameters, diagnostic)


def format_secret_error(error: BaseException) -> str:
    """Translate supported structured secret errors; preserve other diagnostics."""

    if not isinstance(error, SshPilotError) or error.code not in _SECRET_ERROR_TEMPLATES:
        return str(error)
    backend = error.details.get("backend")
    if not isinstance(backend, str) or not backend:
        raise ValueError("secret backend error is missing its backend parameter")
    return _render(_SECRET_ERROR_TEMPLATES[error.code], {"backend": backend}, "")
