"""Frontend presentation of pre-connection command notices.

The daemon sends codes and numbers; this layer owns every word the user reads.
Two properties matter enough to pin: each failure reason has a sentence (an
unmapped reason would raise into a toast handler and lose the alert), and every
sentence says the connection continues -- because the step deliberately never
vetoes a launch, and a message that omits that reads as "we gave up".
"""

from __future__ import annotations

import pytest

from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandPhase,
    PreCommandReason,
    PreConnectionCommandNotice,
)
from sshpilot.gtk import pre_command_messages as messages


FAILURE_REASONS = (
    PreCommandReason.NONZERO_EXIT,
    PreCommandReason.TIMED_OUT,
    PreCommandReason.START_FAILED,
)

QUIET_REASONS = (PreCommandReason.OK, PreCommandReason.COALESCED)


def _notice(reason, *, phase=PreCommandPhase.FINISHED, exit_code=None):
    return PreConnectionCommandNotice(
        connection_id="conn-1",
        scope_id="scope-1",
        kind=PreCommandLaunchKind.TERMINAL,
        phase=phase,
        reason=reason,
        exit_code=exit_code,
        duration_ms=120,
    )


@pytest.mark.parametrize("reason", FAILURE_REASONS)
def test_every_failure_reason_has_a_sentence(reason):
    notice = _notice(reason, exit_code=3 if reason is PreCommandReason.NONZERO_EXIT else None)

    message = messages.format_pre_command_failure(notice, display_name="prod-db")

    assert "prod-db" in message
    assert message.strip()


@pytest.mark.parametrize("reason", FAILURE_REASONS)
def test_every_failure_sentence_says_the_connection_continues(reason):
    """Never abort is the contract; the wording has to carry it."""

    notice = _notice(reason, exit_code=1)

    assert "Connecting anyway" in messages.format_pre_command_failure(notice)


def test_a_non_zero_exit_reports_its_status():
    notice = _notice(PreCommandReason.NONZERO_EXIT, exit_code=42)

    assert "42" in messages.format_pre_command_failure(notice, display_name="host")


@pytest.mark.parametrize("reason", QUIET_REASONS)
def test_success_and_coalescing_are_not_failures(reason):
    assert not messages.pre_command_is_failure(_notice(reason))


def test_a_running_notice_is_not_a_failure():
    assert not messages.pre_command_is_failure(
        _notice(PreCommandReason.OK, phase=PreCommandPhase.RUNNING)
    )


@pytest.mark.parametrize("reason", QUIET_REASONS)
def test_formatting_a_non_failure_is_refused(reason):
    with pytest.raises(ValueError):
        messages.format_pre_command_failure(_notice(reason))


def test_an_unnamed_connection_falls_back_to_its_id():
    notice = _notice(PreCommandReason.TIMED_OUT)

    assert "conn-1" in messages.format_pre_command_failure(notice, display_name="   ")


def test_the_running_line_is_not_empty():
    assert messages.format_pre_command_running().strip()
