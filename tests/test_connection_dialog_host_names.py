"""The alias field read as a Host line: one name, several, or a pattern."""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError, ValidationReason
from sshpilot.connection_dialog_host_names import (
    EMPTY, PATTERN, SEVERAL, SINGLE, handoff_options, read_host_names,
)
from sshpilot.gtk.connection_save_messages import save_failure_text


@pytest.mark.parametrize("text,kind,names", [
    ("", EMPTY, []),
    ("   ", EMPTY, []),
    ("web", SINGLE, ["web"]),
    ("  web  ", SINGLE, ["web"]),
    ("web1 web2", SEVERAL, ["web1", "web2"]),
    ("web1\tweb2   web3", SEVERAL, ["web1", "web2", "web3"]),
    ("*", PATTERN, ["*"]),
    ("*.prod", PATTERN, ["*.prod"]),
    ("web?", PATTERN, ["web?"]),
    ("!bastion", PATTERN, ["!bastion"]),
    ("*.prod edge", PATTERN, ["*.prod", "edge"]),
    ("edge !bad", PATTERN, ["edge", "!bad"]),
])
def test_what_is_typed_is_one_name_several_or_a_pattern(text, kind, names):
    assert read_host_names(text) == (kind, names)


def test_the_block_handed_over_carries_what_was_typed_beside_the_alias():
    assert handoff_options(" 10.0.0.5 ", "deploy", "2222") == [
        ("HostName", "10.0.0.5"), ("User", "deploy"), ("Port", "2222"),
    ]
    # Nothing typed is nothing written, and 22 is what OpenSSH uses anyway.
    assert handoff_options("", "", "22") == []
    assert handoff_options("", "deploy", "") == [("User", "deploy")]


def test_a_pattern_the_daemon_refused_is_worded_for_the_user():
    refused = SshPilotError(
        ErrorCode.VALIDATION_FAILED, "invalid",
        details={"reason": ValidationReason.HOST_PATTERN.value},
    )
    assert "Advanced Configuration" in save_failure_text(refused)
    assert save_failure_text(SshPilotError(ErrorCode.VALIDATION_FAILED, "invalid")) == ""
    unknown = SshPilotError(ErrorCode.VALIDATION_FAILED, "invalid", details={"reason": "x"})
    assert save_failure_text(unknown) == ""
