"""Shared legacy/daemon connection-evidence policy."""

import pytest

from sshpilot.core.connection_evidence import (
    ConnectionEvidence,
    classify_connection_evidence,
)


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


@pytest.mark.parametrize(
    "line",
    [
        # Multiplexing, as OpenSSH 10 prints it: a master that cannot listen
        # makes ssh exit 255 after authenticating.
        "unix_listener: cannot bind to path /home/u/cm/35b5.hXhvnOwTeT9kHHHs: "
        "No such file or directory",
        'unix_listener: path "/home/u/cm/35b5.wvbwpUUUKwLzx" too long for Unix '
        "domain socket",
        "ControlPath too long ('/home/u/cm/35b5' >= 108 bytes)",
        "command-line line 0: Bad ControlPersist argument.",
    ],
)
def test_multiplexing_failures_are_failed_with_ssh_s_line(line):
    from sshpilot.core.connection_evidence import post_auth_exit_failure_reason

    evidence = classify_connection_evidence(line + "\r\n")
    assert evidence.verdict == "failed"
    assert evidence.failure_reason == line
    assert post_auth_exit_failure_reason(line + "\r\n", exit_code=255) == line


def test_a_refused_config_line_is_named_over_ssh_s_summary():
    from sshpilot.core.connection_evidence import post_auth_exit_failure_reason

    text = (
        "/home/u/.ssh/config line 4: Bad ControlPersist argument.\r\n"
        "/home/u/.ssh/config: terminating, 1 bad configuration options\r\n"
    )
    named = "/home/u/.ssh/config line 4: Bad ControlPersist argument."
    assert classify_connection_evidence(text) == ConnectionEvidence("failed", named)
    assert post_auth_exit_failure_reason(text, exit_code=255) == named
    # Alone, the summary is still a failure.
    summary = "/home/u/.ssh/config: terminating, 1 bad configuration options"
    assert classify_connection_evidence(summary).verdict == "failed"
