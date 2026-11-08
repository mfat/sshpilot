"""Reading FreeRDP's failure sentence for the RDP tab."""

import pytest

from sshpilot.rdp_failure import RdpFailureKind, classify_failure


@pytest.mark.parametrize(
    "message",
    [
        # FreeRDP's ERRCONNECT_* strings (libfreerdp/core/errconnect.c).
        "Logon failed.",
        "Wrong password supplied.",
        "Credentials invalid or missing.",
        "Access denied.",
        "The account is disabled.",
        "Account restriction.",
        "Account locked out.",
        "Account expired.",
        "The password has expired and must be changed.",
        "The password has certainly expired and must be changed.",
        "The password must be changed.",
        "Logon type not granted.",
        # Authentication failures that are not about one password.
        "An authentication failure aborted the connection.",
        "Authentication failed. This may be due to NTLM authentication being blocked "
        "by the server's security policy, or a CredSSP encryption oracle mismatch.",
    ],
)
def test_authentication_sentences(message):
    assert classify_failure(message) is RdpFailureKind.AUTHENTICATION


@pytest.mark.parametrize(
    "message",
    [
        "The connection failed.",
        "The connection transport layer failed.",
        "The connection failed at TLS connect.",
        "The connection failed at negotiating security settings.",
        "The connection failed at initial MCS connect",
        "The DNS entry could not be resolved.",
        "The DNS host name was not found.",
    ],
)
def test_connection_sentences(message):
    assert classify_failure(message) is RdpFailureKind.UNREACHABLE


def test_a_cancelled_connect_is_not_a_failure_reason():
    assert classify_failure("The connection was cancelled.") is RdpFailureKind.CANCELLED


def test_the_server_logging_the_session_off_is_not_an_authentication_failure():
    # What a rejected logon looked like on the reporter's xrdp host: FreeRDP
    # reported the session as connected and then logged off, not as refused.
    assert classify_failure(
        "The disconnection was initiated by the user logging off their session on the server."
    ) is RdpFailureKind.OTHER


@pytest.mark.parametrize("message", [None, "", "   ", "Something FreeRDP never said."])
def test_unknown_sentences_stay_unknown(message):
    assert classify_failure(message) is RdpFailureKind.OTHER


def test_a_code_prefix_or_different_case_still_classifies():
    assert classify_failure(
        "ERRCONNECT_CONNECT_FAILED [0x00020006]: The connection failed."
    ) is RdpFailureKind.UNREACHABLE
    assert classify_failure("LOGON FAILED.") is RdpFailureKind.AUTHENTICATION


def test_authentication_is_checked_before_connection():
    # "An authentication failure aborted the connection." mentions the
    # connection but is an authentication failure.
    assert classify_failure(
        "An authentication failure aborted the connection."
    ) is RdpFailureKind.AUTHENTICATION
