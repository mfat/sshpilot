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
    State.SHADOWED: "error",            # does not take effect: red, as in Effective
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
        return _("No effect: {where} sets this option first.").format(
            where=where(item.decided_file, item.decided_line, item.decided_block)
        )
    if state is State.ACCUMULATED:
        return _("OpenSSH collects every value of this option.")
    if state is State.INACTIVE:
        return _("This block does not apply to the target.")
    if state is State.FORCED:
        return _("No effect: Settings ▸ SSH Options set this option for every connection.")
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
            "names. Search for a host name in the sidebar to see what OpenSSH "
            "would use for it."
        )
    shown = ", ".join(hosts[:12]) + (", …" if len(hosts) > 12 else "")
    return _(
        "The picked block applies to: {hosts}. Pick one of them to see what "
        "OpenSSH would use for it."
    ).format(hosts=shown)


def applies_to_label() -> str:
    """Before the hosts a pattern or Match block applies to."""
    return _("Applies to:")


def applies_to_none() -> str:
    return _("Applies to none of the hosts this configuration names.")


def show_host_block(host: str) -> str:
    """The tooltip of a host a pattern block applies to: a link to it."""
    return _("Show the block of {host}").format(host=host)


def no_host_text() -> str:
    """While the block picked names no host and none is kept in question."""
    return _("Pick a block that names a host to see what OpenSSH would use for it.")


def block_title(kind: str, patterns: str) -> str:
    """A block as the sidebar names it: its own first line."""
    if kind == "global":
        return _("Global")
    # Host and Match are OpenSSH's keywords: not translated.
    return ("Host " if kind == "host" else "Match ") + patterns.strip()


def block_list_title(kind: str, patterns: str) -> str:
    """A block's name in a list of blocks: what follows its keyword. The
    row's caption says what kind of block it is."""
    if kind == "global":
        return _("Global Options")
    if kind == "match":
        # Match is OpenSSH's keyword: not translated.
        return "Match " + patterns.strip()
    if patterns.split() == ["*"]:
        # A star alone says little: with its keyword, as it is written.
        return "Host *"
    return patterns.strip() or _("New Host Block")


def block_category_label(category: str, names: int = 1, kind: str = "") -> str:
    """What a block is for, as the caption of its row. *kind* tells
    ``Host *`` (a wildcard Host block) from the global options and
    ``Match all``, which apply to every host as well."""
    if category == "connection":
        # The file's own word: a "connection" is the app's name for a host
        # it lists, and this is a view of the file.
        if names <= 1:
            return _("Host")
        return ngettext("{count} host", "{count} hosts", names).format(count=names)
    if category == "pattern":
        return _("Host pattern")
    if category == "match_rule":
        return _("Match rule")
    if kind == "host":
        # Host * (Host is OpenSSH's keyword: not translated).
        return _("Wildcard Host block")
    return _("All hosts")


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


#: A command longer than this is cut in the prompt: it is there to be
#: recognised, and the whole of it is in the draft.
_COMMAND_SHOWN = 240


def pending_exec_text(evaluation: SshConfigDraftEvaluation, host: str = "") -> str:
    """What allowing would run, and for which host: the commands
    themselves, word for word, so that nothing has to be looked for in the
    draft before saying yes."""
    if not host:
        return pending_exec_many_hosts_text()
    commands = "\n".join(
        command if len(command) <= _COMMAND_SHOWN else command[:_COMMAND_SHOWN] + "…"
        for command in evaluation.pending_exec
    )
    count = len(evaluation.pending_exec)
    return ngettext(
        "This draft adds a Match exec command. OpenSSH runs it on this "
        "computer to evaluate the block, so nothing is evaluated until you "
        "allow it for “{host}”:\n{commands}",
        "This draft adds {count} Match exec commands. OpenSSH runs them on "
        "this computer to evaluate their blocks, so nothing is evaluated "
        "until you allow them for “{host}”:\n{commands}",
        count,
    ).format(count=count, host=host, commands=commands)


def pending_exec_many_hosts_text() -> str:
    return _(
        "This draft adds a Match exec command, which OpenSSH would run for "
        "every host this block is checked against. Select or type one host "
        "to allow it for that host."
    )


def used_instead_markup(value: str, place: str, forced: bool = False) -> str:
    """Pango markup, in place of a row's description while its line goes
    unused: the value OpenSSH takes, and where it takes it from."""
    from xml.sax.saxutils import escape

    shown = f"<tt>{escape(value)}</tt>"
    if forced:
        # Translators: {value} is an option's value, e.g. "Settings ▸ SSH Options use 7".
        return escape(_("Settings ▸ SSH Options use {value}")).replace("{value}", shown)
    # Translators: e.g. "OpenSSH uses 10.0.0.5 from line 3 (Host zoo)".
    return escape(_("OpenSSH uses {value} from {place}")).replace(
        "{value}", shown).replace("{place}", escape(place))


def used_instead_text(value: str, place: str, forced: bool = False) -> str:
    """:func:`used_instead_markup` as plain text, for a tooltip."""
    if forced:
        return _("Settings ▸ SSH Options use {value}").format(value=value)
    return _("OpenSSH uses {value} from {place}").format(value=value, place=place)


def inherited_text(value: str, source, *, forced: bool = False,
                   default: bool = False) -> str:
    """What a field the block leaves empty gets, and from where: worded as
    a line that goes unused is, since the value is decided the same way."""
    if forced:
        return used_instead_text(value, "", forced=True)
    if source is not None:
        return used_instead_text(
            value, where(source.file, source.line, source.block)
        )
    if default:
        # Translators: {value} is an option's value, e.g. "OpenSSH default: 22".
        return _("OpenSSH default: {value}").format(value=value)
    return ""


def jump_text(place: str) -> str:
    """The toast after following a link: where it went (Back returns)."""
    # Translators: e.g. "Showing line 2 (Host zoo)".
    return _("Showing {place}").format(place=place)


def show_line_hint() -> str:
    """Under a "No effect" pill's reason: the pill is a link to that line."""
    return _("Click to show that line.")


def open_file_tooltip() -> str:
    return _("Show this file here")


def default_source() -> str:
    return _("OpenSSH default")


def forced_source() -> str:
    return _("Settings ▸ SSH Options")


def unknown_source() -> str:
    """A value no line this window can see accounts for."""
    return _("Set by the configuration")


def value_problem_text(file: str, problem: str) -> str:
    """A file an Effective value names, and what is wrong with it."""
    # Translators: e.g. "~/.ssh/id_work: File not found".
    return _("{file}: {problem}").format(file=file, problem=problem)


def problem_count(count: int) -> str:
    return ngettext("{count} problem", "{count} problems", count).format(count=count)


def asked_text(host: str) -> str:
    """Under the command line Effective sums up: what OpenSSH was asked."""
    return _("What OpenSSH resolves “{host}” to:").format(host=host)


def alias_text(host: str) -> str:
    """The name a connection is known by, when it differs from the address."""
    return _("alias {host}").format(host=host)


def route_start() -> str:
    """The start of a route through jump hosts: this computer."""
    # Translators: the first step of a route, e.g. "you → bastion → db1".
    return _("you")


def direct_route() -> str:
    return _("Direct")


def no_jump_host() -> str:
    return _("No jump host or proxy command")


def default_keys() -> str:
    """No key file is set: OpenSSH tries its own (id_ed25519, id_rsa...)."""
    return _("OpenSSH default keys")


def and_more(first: str, more: int) -> str:
    """The first of several places, and how many others there are."""
    # Translators: e.g. "line 3 (Host web1) and 2 more".
    return ngettext(
        "{first} and {count} more", "{first} and {count} more", more
    ).format(first=first, count=more)


def overridden_count(count: int) -> str:
    """Lines of an option that Settings ▸ SSH Options override."""
    return ngettext(
        "{count} line overridden by Settings", "{count} lines overridden by Settings", count
    ).format(count=count)


def shadowed_count(count: int) -> str:
    """Short, for a row's problem label (Effective)."""
    return ngettext(
        "{count} line has no effect", "{count} lines have no effect", count
    ).format(count=count)


def precedence_problem(count: int) -> str:
    """The title card's Problems tile: directives an earlier one beats."""
    return ngettext(
        "{count} directive will not take effect due to configuration precedence.",
        "{count} directives will not take effect due to configuration precedence.",
        count,
    ).format(count=count)


def settings_problem(count: int) -> str:
    """The title card's Problems tile: directives Settings override."""
    return ngettext(
        "{count} directive is overridden by Settings ▸ SSH Options.",
        "{count} directives are overridden by Settings ▸ SSH Options.",
        count,
    ).format(count=count)


def rejected_problem(count: int) -> str:
    """The title card's Problems tile: directives OpenSSH rejects."""
    return ngettext(
        "{count} directive is rejected by OpenSSH.",
        "{count} directives are rejected by OpenSSH.",
        count,
    ).format(count=count)


# -- the Structured view --------------------------------------------------------

def source_label(source, *, forced: bool = False, default: bool = False) -> str:
    """Where a value this block does not set comes from, for the label
    beside an empty field: another block, another file, Settings, or
    OpenSSH's own default."""
    if forced:
        return forced_source()
    if source is None:
        return default_source() if default else ""
    if source.block:
        # The block's header as written, e.g. "from Host *".
        return _("from {block}").format(block=source.block)
    if source.file:
        return _("from {file}").format(file=source.file)
    return _("from the global options")


def applies_text(applies, host: str) -> str:
    """Whether the block on screen applies to the host being checked."""
    if applies is None or not host:
        return ""
    if applies:
        return _("Applies to {host}.").format(host=host)
    return _(
        "Does not apply to {host}: OpenSSH skips this block for it."
    ).format(host=host)


def key_status_text(state: str, mode: int = 0) -> str:
    """What the key file on disk says about itself (see
    ``advanced_config_fields.key_file_status``)."""
    if state == "missing":
        return _("File not found")
    if state == "open":
        return _("Permissions {mode}: other users can read it").format(mode=f"{mode:04o}")
    if state == "unchecked":
        return _("Checked when connecting")
    return ""
