"""Frontend-owned presentation of structured identity-operation failures."""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.operations import IdentityFailure, IdentityFailureCode
from ..i18n import N_


_IDENTITY_FAILURE_TEMPLATES = {
    IdentityFailureCode.LAUNCH_PREPARATION_UNAVAILABLE: N_(
        "Public-key deployment is unavailable."
    ),
    IdentityFailureCode.CONNECTION_NOT_FOUND: N_(
        "The selected connection no longer exists in the daemon."
    ),
    IdentityFailureCode.SSH_CONNECTION_REQUIRED: N_(
        "Public-key deployment requires an SSH connection."
    ),
    IdentityFailureCode.HOST_IDENTIFIER_MISSING: N_(
        "The connection has no host or SSH alias."
    ),
    IdentityFailureCode.SSH_COPY_ID_UNAVAILABLE: N_(
        "ssh-copy-id is not installed."
    ),
    IdentityFailureCode.PROCESS_START_FAILED: N_(
        "ssh-copy-id could not be started."
    ),
    IdentityFailureCode.AUTHENTICATION_FAILED: N_(
        "Authentication failed while installing the public key"
    ),
    IdentityFailureCode.CONNECTION_REFUSED: N_("The server refused the connection"),
    IdentityFailureCode.SERVER_UNREACHABLE: N_("The server could not be reached"),
    IdentityFailureCode.CONNECTION_TIMED_OUT: N_("The connection timed out"),
    IdentityFailureCode.INSTALLATION_FAILED: N_(
        "ssh-copy-id could not install the public key"
    ),
    IdentityFailureCode.OPERATION_FAILED_UNEXPECTEDLY: N_(
        "The operation failed unexpectedly"
    ),
}


def format_identity_failure(failure: IdentityFailure) -> str:
    """Translate one identity failure and append its diagnostic unchanged."""

    if type(failure) is not IdentityFailure:
        raise ValueError("invalid identity failure")
    try:
        template = _IDENTITY_FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError(
            "identity failure code has no frontend presentation"
        ) from None
    message = _(template).format(**failure.parameters)
    return f"{message}\n\n{failure.diagnostic}" if failure.diagnostic else message
