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
    PreCommandTestResult,
    PreConnectionCommandNotice,
)
from ..i18n import N_


#: Shown while the command is still running, so a slow knock or VPN dial-up is
#: not a silent multi-second hang on connect.
PRE_COMMAND_RUNNING = N_("Running pre-connection command…")

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
    template = templates[notice.reason]
    name = display_name.strip() or str(notice.connection_id)
    return _(template).format(
        name=name,
        code=notice.exit_code if notice.exit_code is not None else "?",
    )


def format_pre_command_running() -> str:
    return _(PRE_COMMAND_RUNNING)


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
    try:
        template = _TEST_TEMPLATES[result.reason]
    except KeyError:
        raise ValueError("pre-connection command test result has no presentation")
    text = _(template).format(
        code=result.exit_code if result.exit_code is not None else "?",
        seconds=f"{result.duration_ms / 1000:.1f}",
    )
    output = result.output.strip()
    if output:
        text = f"{text}\n{output}"
    return text, result.succeeded


__all__ = [
    "PRE_COMMAND_RUNNING",
    "format_pre_command_failure",
    "format_pre_command_running",
    "format_pre_command_test",
    "pre_command_is_failure",
]
