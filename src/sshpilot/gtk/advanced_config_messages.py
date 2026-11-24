"""Wording for Advanced Configuration (GTK-free).

The daemon reports stable codes and positions; everything a person reads is
worded and translated here. OpenSSH's own diagnostics are appended as they
are, and SSH keywords and file names are never translated.
"""
from __future__ import annotations

from gettext import gettext as _, ngettext
from typing import Optional

from ..api.models.connections import (
    SshConfigDraftError,
    SshConfigDraftEvaluation,
    SshConfigDraftLine,
    SshConfigDraftLineState as State,
)
from ..i18n import N_

_STATE_LABELS = {
    State.APPLIED: N_("Applied"),
    State.SHADOWED: N_("Shadowed"),
    State.ACCUMULATED: N_("Accumulated"),
    State.INACTIVE: N_("Inactive"),
    State.FORCED: N_("Overridden"),
}
# An OpenSSH keyword: shown as it is.
_INCLUDE_LABEL = "Include"
#: The pill style of each state (see ``advanced_config_window``).
STATE_STYLES = {
    State.APPLIED: "success",
    State.SHADOWED: "warning",
    State.ACCUMULATED: "accent",
    State.INACTIVE: "muted",
    State.FORCED: "purple",
    State.INCLUDE: "accent",
}


def state_label(state: State) -> str:
    if state is State.INCLUDE:
        return _INCLUDE_LABEL
    return _(_STATE_LABELS[state])


def where(file: str, line: int, block: str) -> str:
    """A line's place: in this configuration, or in another file."""
    if file:
        if block:
            return _("{file}, line {line} ({block})").format(
                file=file, line=line, block=block
            )
        return _("{file}, line {line}").format(file=file, line=line)
    if block:
        return _("line {line} ({block})").format(line=line, block=block)
    return _("line {line} (global options)").format(line=line)


def state_tooltip(item: SshConfigDraftLine) -> str:
    """Why an option line has its state."""
    state = item.state
    if state is State.APPLIED:
        return _("OpenSSH takes this option's value from this line.")
    if state is State.SHADOWED:
        return _("Not used: {where} sets this option first.").format(
            where=where(item.decided_file, item.decided_line, item.decided_block)
        )
    if state is State.ACCUMULATED:
        return _("OpenSSH collects every value of this option.")
    if state is State.INACTIVE:
        return _("This block does not apply to the target.")
    if state is State.FORCED:
        return _("Not used: Settings ▸ SSH Options set this option for every connection.")
    return _("OpenSSH reads the files this line names at this place.")


def block_status(disabled: bool, matched: Optional[bool], is_global: bool) -> tuple:
    """``(label, style, tooltip)`` of a block's pill; ``matched`` is None
    (and the label empty) while there is no answer from OpenSSH."""
    if disabled:
        return (_("Disabled"), "muted",
                _("Written as comments: OpenSSH does not read this block."))
    if matched is None:
        return ("", "muted", "")      # no host is being filtered by
    if is_global:
        return (_("Always"), "success",
                _("Options above every Host and Match line apply to every target."))
    if matched:
        return (_("Matched"), "success", _("OpenSSH applies this block to the target."))
    # A block that does not apply is dimmed and carries no word: "skipped"
    # read as a verdict on the block, when it is only not this host's.
    return ("", "muted", "")


def related_status(disabled: bool, related: Optional[bool], is_global: bool) -> tuple:
    """A block's pill while the block picked names no host of its own (a
    pattern, a Match rule): whether the two apply to a host in common."""
    if disabled or related is None or is_global:
        return block_status(disabled, related, is_global)
    if related:
        return (_("Matched"), "success",
                _("Applies to a host the picked block applies to."))
    return ("", "muted", "")


def related_note(hosts) -> str:
    """What the pages say while the block picked names no host of its own."""
    if not hosts:
        return _(
            "The picked block applies to none of the hosts this configuration "
            "names. Type a host in the sidebar to see what OpenSSH would use for it."
        )
    shown = ", ".join(hosts[:12]) + (", …" if len(hosts) > 12 else "")
    return _(
        "The picked block applies to: {hosts}. Blocks that apply to any of "
        "them are marked. Pick or type one host to see what OpenSSH would "
        "use for it."
    ).format(hosts=shown)


def block_title(kind: str, patterns: str) -> str:
    """A block as the sidebar names it: its own first line."""
    if kind == "global":
        return _("Global")
    # Host and Match are OpenSSH's keywords: not translated.
    return ("Host " if kind == "host" else "Match ") + patterns.strip()


def matched_count(count: int) -> str:
    return ngettext("{count} matched", "{count} matched", count).format(count=count)


def block_count(count: int) -> str:
    return ngettext("{count} block", "{count} blocks", count).format(count=count)


def error_text(error: SshConfigDraftError) -> str:
    """One rejected line, with OpenSSH's own words."""
    if error.file and error.line:
        prefix = _("OpenSSH rejects {file}, line {line}").format(
            file=error.file, line=error.line
        )
    elif error.line:
        prefix = _("OpenSSH rejects line {line}").format(line=error.line)
    else:
        prefix = _("OpenSSH rejects this configuration")
    return f"{prefix}: {error.diagnostic}" if error.diagnostic else prefix


def pending_exec_text(evaluation: SshConfigDraftEvaluation) -> str:
    count = len(evaluation.pending_exec)
    return ngettext(
        "This draft adds a Match exec command. OpenSSH runs it to evaluate "
        "the block, so nothing is evaluated until you allow it.",
        "This draft adds {count} Match exec commands. OpenSSH runs them to "
        "evaluate their blocks, so nothing is evaluated until you allow it.",
        count,
    ).format(count=count)


def pending_exec_many_hosts_text() -> str:
    return _(
        "This draft adds a Match exec command, which OpenSSH would run for "
        "every host this block is checked against. Select or type one host "
        "to allow it for that host."
    )


def default_source() -> str:
    return _("OpenSSH default")


def forced_source() -> str:
    return _("Settings ▸ SSH Options")


def unknown_source() -> str:
    """A value no line this window can see accounts for."""
    return _("Set by the configuration")


def shadowed_count(count: int) -> str:
    return ngettext(
        "{count} line not used", "{count} lines not used", count
    ).format(count=count)
