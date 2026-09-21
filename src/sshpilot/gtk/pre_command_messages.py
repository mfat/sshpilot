"""Frontend-owned presentation of pre-connection command notices.

The daemon publishes codes and numbers, plus a failed command's own output
(see :mod:`sshpilot.api.models.pre_command`). The *wording* lives here, so it
can be translated and so the command text -- which may carry a token or a
password -- never has to cross the wire to be rendered.

Each failure sentence says what happened next, because that is the part a user
cannot infer. By default the connection continues, and someone reading "the
pre-connection command failed" without that clause will reasonably assume it
was abandoned and stop waiting. When the connection asked to be gated on the
command, the opposite is true and the sentence has to say so -- telling someone
the connection continues when it did not is worse than saying nothing.
"""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.pre_command import (
    PreCommandPhase,
    PreCommandReason,
    PreCommandStage,
    PreCommandTestResult,
    PreConnectionCommandNotice,
)
from ..i18n import N_


#: Shown while the command is still running, so a slow knock or VPN dial-up is
#: not a silent multi-second hang on connect.
PRE_COMMAND_RUNNING = N_("Running pre-connection command…")

#: The knock half, which most connections using this feature are the *only*
#: user of. Calling that "a pre-connection command" would name a thing they
#: never configured.
PRE_COMMAND_KNOCKING = N_("Sending the port knock…")

_RUNNING_TEXTS = {
    PreCommandStage.KNOCK: PRE_COMMAND_KNOCKING,
    PreCommandStage.COMMAND: PRE_COMMAND_RUNNING,
}

#: A knock has no process, so it cannot exit non-zero or time out; the only
#: way it fails is by not being sendable at all. One entry is therefore the
#: whole table, and the ``.get`` below falls back to the command wording if a
#: future reason ever reaches here.
_KNOCK_ABORTED_TEMPLATES = {
    PreCommandReason.START_FAILED: N_(
        "The port knock for “{name}” could not be sent, so SSH Pilot did not "
        "connect."
    ),
}

_KNOCK_FAILURE_TEMPLATES = {
    PreCommandReason.START_FAILED: N_(
        "The port knock for “{name}” could not be sent. Connecting anyway."
    ),
}

#: Said instead of "Connecting anyway" when the connection asked to be gated
#: on this command. Telling someone the connection continues when it did not
#: is worse than saying nothing.
_PRE_COMMAND_ABORTED_TEMPLATES = {
    PreCommandReason.NONZERO_EXIT: N_(
        "The pre-connection command for “{name}” failed (exit {code}), so "
        "SSH Pilot did not connect."
    ),
    PreCommandReason.TIMED_OUT: N_(
        "The pre-connection command for “{name}” timed out, so SSH Pilot did "
        "not connect."
    ),
    PreCommandReason.START_FAILED: N_(
        "The pre-connection command for “{name}” could not be started, so "
        "SSH Pilot did not connect."
    ),
}

_PRE_COMMAND_FAILURE_TEMPLATES = {
    PreCommandReason.NONZERO_EXIT: N_(
        "The pre-connection command for “{name}” failed (exit {code}). "
        "Connecting anyway."
    ),
    PreCommandReason.TIMED_OUT: N_(
        "The pre-connection command for “{name}” timed out. Connecting anyway."
    ),
    PreCommandReason.START_FAILED: N_(
        "The pre-connection command for “{name}” could not be started. "
        "Connecting anyway."
    ),
}


def pre_command_is_failure(notice: PreConnectionCommandNotice) -> bool:
    """Whether *notice* is a finished attempt the user should hear about.

    ``OK`` and ``COALESCED`` are finished too; neither is worth interrupting
    anyone for.
    """

    if type(notice) is not PreConnectionCommandNotice:
        raise ValueError("invalid pre-connection command notice")
    return (
        notice.phase is PreCommandPhase.FINISHED
        and notice.reason in _PRE_COMMAND_FAILURE_TEMPLATES
    )


def format_pre_command_failure(
    notice: PreConnectionCommandNotice,
    *,
    display_name: str = "",
) -> str:
    """Translate one failed pre-connection command notice.

    *display_name* is the connection's nickname when the caller has it; the
    connection id is a poor substitute but better than an unnamed host, and a
    toast with no subject at all is close to useless.
    """

    if not pre_command_is_failure(notice):
        raise ValueError("pre-connection command notice is not a failure")
    templates = (
        _PRE_COMMAND_ABORTED_TEMPLATES
        if notice.aborted
        else _PRE_COMMAND_FAILURE_TEMPLATES
    )
    if notice.stage is PreCommandStage.KNOCK:
        knock_templates = (
            _KNOCK_ABORTED_TEMPLATES
            if notice.aborted
            else _KNOCK_FAILURE_TEMPLATES
        )
        templates = {**templates, **knock_templates}
    template = templates[notice.reason]
    name = display_name.strip() or str(notice.connection_id)
    return _(template).format(
        name=name,
        code=notice.exit_code if notice.exit_code is not None else "?",
    )


def format_pre_command_running(
    stage: PreCommandStage = PreCommandStage.COMMAND,
) -> str:
    """The status text for whichever half is actually running."""

    return _(_RUNNING_TEXTS.get(stage, PRE_COMMAND_RUNNING))


#: The knock's own Test wording. "Ran successfully" would overclaim: a knock
#: is never acknowledged, so all we can honestly report is that the packets
#: went out. Whether the firewall opened is only knowable by connecting.
_KNOCK_TEST_TEMPLATES = {
    PreCommandReason.OK: N_("Sent in {seconds}s."),
    PreCommandReason.START_FAILED: N_("Could not be sent."),
}

_TEST_TEMPLATES = {
    PreCommandReason.OK: N_("Ran successfully in {seconds}s."),
    PreCommandReason.NONZERO_EXIT: N_("Exited with status {code} after {seconds}s."),
    PreCommandReason.TIMED_OUT: N_("Timed out after {seconds}s."),
    PreCommandReason.START_FAILED: N_("Could not be started."),
}


def format_pre_command_test(result: PreCommandTestResult) -> tuple:
    """Render a Test result as ``(text, succeeded)``.

    The command's own output is appended when there is any: the user pressed
    Test to see it, and a knock that failed is usually only diagnosable from
    what the tool printed.
    """

    if type(result) is not PreCommandTestResult:
        raise ValueError("invalid pre-connection command test result")
    templates = _TEST_TEMPLATES
    if result.stage is PreCommandStage.KNOCK:
        templates = {**_TEST_TEMPLATES, **_KNOCK_TEST_TEMPLATES}
    try:
        template = templates[result.reason]
    except KeyError:
        raise ValueError(
            "pre-connection command test result has no presentation"
        ) from None
    text = _(template).format(
        code=result.exit_code if result.exit_code is not None else "?",
        seconds=f"{result.duration_ms / 1000:.1f}",
    )
    output = result.output.strip()
    if output:
        text = f"{text}\n{output}"
    return text, result.succeeded


__all__ = [
    "PRE_COMMAND_KNOCKING",
    "PRE_COMMAND_RUNNING",
    "format_pre_command_failure",
    "format_pre_command_running",
    "format_pre_command_test",
    "pre_command_is_failure",
]
