"""What a failed RDP connection means, from FreeRDP's own sentence.

FreeRDP reports a failed connect as one English sentence produced by
``freerdp_get_last_error_string()``. gtk4-frdp passes that sentence on
``rdp-auth-failure`` for both authentication and connection failures (see
``frdp_session_report_connect_failure`` in subprojects/gtk4-frdp), so the tab
cannot trust the signal name: it has to read the sentence to say "the password
was rejected" or "the host could not be reached".

The sentences are FreeRDP's ``ERRCONNECT_*`` strings from
``libfreerdp/core/errconnect.c``. FreeRDP does not translate them, so the
markers below are English on every system; the words sshPilot shows are its
own. Matching is case-insensitive and by substring, so a future FreeRDP that
prefixes the code (``ERRCONNECT_CONNECT_FAILED [0x00020006]: The connection
failed.``) still classifies.

FreeRDP loses the specific error in one case worth knowing about: when the
server closes the connection during the security handshake, ``freerdp_connect``
records ``FREERDP_ERROR_CONNECT_TRANSPORT_FAILED`` and then silently retries
(``freerdp_reconnect`` clears that error); the retry usually fails at the TCP
level, so the sentence is the generic "The connection failed." even though the
server had rejected the logon. sshPilot therefore offers the password prompt
for that kind too, not only for the explicit authentication sentences.
"""

from __future__ import annotations

import enum
from typing import Optional


class RdpFailureKind(enum.Enum):
    """How a failed RDP connect ended."""

    AUTHENTICATION = "authentication"
    #: The session was never established: TCP, TLS, negotiation or DNS.
    UNREACHABLE = "unreachable"
    CANCELLED = "cancelled"
    #: A server-side end (logoff, policy, timeout, ...) or an unknown sentence.
    OTHER = "other"


#: FreeRDP sentences that say the logon itself was refused.
_AUTHENTICATION = (
    "authentication fail",              # ERR_CONNECT_AUTHENTICATION_FAILED, NTLM blocked
    "logon failed",                     # ERRCONNECT_LOGON_FAILURE
    "wrong password",                   # ERRCONNECT_WRONG_PASSWORD
    "credentials invalid or missing",   # ERRCONNECT_NO_OR_MISSING_CREDENTIALS
    "access denied",                    # ERRCONNECT_ACCESS_DENIED
    "account is disabled",              # ERRCONNECT_ACCOUNT_DISABLED
    "account restriction",              # ERRCONNECT_ACCOUNT_RESTRICTION
    "account locked out",               # ERRCONNECT_ACCOUNT_LOCKED_OUT
    "account expired",                  # ERRCONNECT_ACCOUNT_EXPIRED
    "password has expired",             # ERRCONNECT_PASSWORD_EXPIRED
    "password has certainly expired",   # ERRCONNECT_PASSWORD_CERTAINLY_EXPIRED
    "password must be changed",         # ERRCONNECT_PASSWORD_MUST_CHANGE
    "logon type not granted",           # ERRCONNECT_LOGON_TYPE_NOT_GRANTED
)

#: FreeRDP sentences that say the session could not be reached at all.
_UNREACHABLE = (
    "connection failed",                # CONNECT_FAILED, TLS, security negotiation, MCS
    "transport layer failed",           # ERRCONNECT_CONNECT_TRANSPORT_FAILED
    "dns entry could not be resolved",  # ERRCONNECT_DNS_ERROR
    "dns host name was not found",      # ERRCONNECT_DNS_NAME_NOT_FOUND
    "could not connect",                # funnel/server messages
)

_CANCELLED = (
    "connection was cancelled",         # ERRCONNECT_CONNECT_CANCELLED
)


def classify_failure(message: Optional[str]) -> RdpFailureKind:
    """The kind of failure FreeRDP's ``message`` describes.

    An empty or unknown sentence is ``OTHER``: the caller still shows it, but
    does not claim to know what it means.
    """

    text = (message or "").strip().casefold()
    if not text:
        return RdpFailureKind.OTHER
    if any(marker in text for marker in _CANCELLED):
        return RdpFailureKind.CANCELLED
    if any(marker in text for marker in _AUTHENTICATION):
        return RdpFailureKind.AUTHENTICATION
    if any(marker in text for marker in _UNREACHABLE):
        return RdpFailureKind.UNREACHABLE
    return RdpFailureKind.OTHER


__all__ = ["RdpFailureKind", "classify_failure"]
