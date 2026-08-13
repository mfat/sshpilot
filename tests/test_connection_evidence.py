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
