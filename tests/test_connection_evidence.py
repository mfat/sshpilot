"""Shared legacy/daemon connection-evidence policy."""

import pytest

from sshpilot.core.connection_evidence import classify_connection_evidence


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "pending"),
        ("debug1: Connecting to example.test", "pending"),
        ("alice@example.test's password: ", "pending"),
        ("Permission denied (publickey,password).", "failed"),
        ("debug1: Authenticated to example.test", "connected"),
        ("\x1b[32malice@host\x1b[0m:\x1b[34m~\x1b[0m$ ", "connected"),
        (
            "Error: remote port forwarding failed for listen port 2222.\r\n",
            "failed",
        ),
        ("bind [127.0.0.1]:8080: Address already in use\r\n", "failed"),
    ],
)
def test_classify_connection_evidence_for_raw_daemon_output(text, expected):
    result = classify_connection_evidence(text)
    assert result.verdict == expected
    assert bool(result.failure_reason) is (expected == "failed")


def test_later_remote_output_supersedes_failed_authentication_attempt():
    result = classify_connection_evidence(
        "Permission denied, please try again.\n"
        "alice@example.test's password: \n"
        "alice@host:~$ "
    )

    assert result.verdict == "connected"
    assert result.failure_reason == ""


def test_forward_failure_after_authenticated_is_failed():
    """ExitOnForwardFailure often prints after Authenticated to."""
    result = classify_connection_evidence(
        'debug1: Authenticated to example.test ([127.0.0.1]:22) using "publickey".\r\n'
        "Error: remote port forwarding failed for listen port 2222.\r\n"
    )

    assert result.verdict == "failed"
    assert "port forwarding failed" in result.failure_reason


def test_post_auth_exit_failure_reason_prefers_openssh_line():
    from sshpilot.core.connection_evidence import post_auth_exit_failure_reason

    reason = post_auth_exit_failure_reason(
        'debug1: Authenticated to example.test using "publickey".\r\n'
        "Could not request local forwarding.\r\n",
        exit_code=255,
    )
    assert reason is not None
    assert "local forwarding" in reason.lower()


def test_post_auth_exit_failure_reason_empty_pty_uses_exit_255():
    from sshpilot.core.connection_evidence import post_auth_exit_failure_reason

    assert (
        post_auth_exit_failure_reason("", exit_code=255)
        == "The SSH session exited with status 255"
    )
    assert post_auth_exit_failure_reason("alice@host:~$ ", exit_code=17) is None
