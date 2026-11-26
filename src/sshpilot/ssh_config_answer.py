"""OpenSSH's answer for a configuration, read once for every view of it.

Advanced Configuration (its Directives view) and the connection editor both
put a configuration to OpenSSH through the daemon's draft evaluation, and
both show the same things about it: which value is used and where it comes
from, and, for each line, whether OpenSSH takes it, ignores it because an
earlier line comes first, has it overridden by Settings ▸ SSH Options, or
rejects it. This module is the one reader of that answer and the one place
that turns a line's state into its mark (:class:`LineVerdict`), so the two
cannot say different things about the same line.

No GTK here: the rules are tested headless. Wording comes from
:mod:`sshpilot.gtk.advanced_config_messages`.
"""
from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _
from typing import Dict, List, Optional, Tuple

from .api.models.connections import SshConfigDraftLineState as State
from .connection_dialog_auth import canonical_keyword
from .gtk import advanced_config_messages as messages

#: What OpenSSH does with one line.
USED = "used"              # takes its value (or collects it)
NO_EFFECT = "no_effect"    # an earlier line set the option first
OVERRIDDEN = "overridden"  # Settings ▸ SSH Options decide the option
REJECTED = "rejected"      # refuses the line


@dataclass(frozen=True)
class LineVerdict:
    """One line's mark, as every view shows it.

    *state* is one of the constants above, or None when nothing is known
    (no answer, no such line). *tag* is the short label beside the line
    (empty while the line is used), *style* its colour, *tooltip* the tag's
    tooltip. *note* says what OpenSSH uses instead and from where (plain
    text; *note_markup* the same as Pango markup). *target* is the line
    that wins, ``(file, line)`` with ``""`` for the evaluated file itself,
    for the tag to link to.
    """

    state: Optional[str] = None
    tag: str = ""
    style: str = "muted"
    tooltip: str = ""
    note: str = ""
    note_markup: str = ""
    target: Optional[Tuple[str, int]] = None

    @property
    def unused(self) -> bool:
        """Valid, and not used: struck through and tinted where typed."""
        return self.state in (NO_EFFECT, OVERRIDDEN)


NOTHING = LineVerdict()


class ConfigAnswer:
    """One :class:`SshConfigDraftEvaluation`, read for display.

    Keywords are compared as OpenSSH compares them: lower case, and either
    spelling of a renamed keyword is one setting.
    """

    def __init__(self, evaluation=None) -> None:
        #: The answer when OpenSSH evaluated the text; None otherwise (no
        #: host, a rejected draft, a command waiting for consent).
        self.evaluation = (
            evaluation if evaluation is not None and evaluation.evaluated else None
        )
        self.values: Dict[str, List[str]] = {}
        self.sources: Dict[str, object] = {}
        #: Keyword -> the value Settings ▸ SSH Options force.
        self.forced: Dict[str, str] = {}
        self.defaults: set = set()
        #: Line number -> its :class:`SshConfigDraftLine`.
        self.states: Dict[int, object] = {}
        # A rejected line is said whether or not the rest was evaluated.
        self.errors: Dict[int, object] = {
            error.line: error
            for error in (getattr(evaluation, "errors", ()) or ())
            if not error.file and error.line
        }
        if self.evaluation is None:
            return
        for item in self.evaluation.values:
            self.values.setdefault(canonical_keyword(item.key), []).append(item.value)
        self.sources = {
            canonical_keyword(source.key): source for source in self.evaluation.sources
        }
        for item in self.evaluation.forced:
            self.forced.setdefault(canonical_keyword(item.key), item.value)
        self.defaults = {canonical_keyword(key) for key in self.evaluation.defaults}
        self.states = {item.line: item for item in self.evaluation.lines}

    @property
    def available(self) -> bool:
        """OpenSSH evaluated the text; otherwise no value is known."""
        return self.evaluation is not None

    # -- values ------------------------------------------------------------

    def used(self, keyword: str) -> str:
        """The value OpenSSH uses for *keyword*, every value joined for an
        option it collects; empty when there is none or no answer."""
        return ", ".join(self.values.get(canonical_keyword(keyword), ()))

    def is_default(self, keyword: str) -> bool:
        return canonical_keyword(keyword) in self.defaults

    def source_text(self, keyword: str) -> str:
        """Where the value of *keyword* comes from, for the label beside a
        field that does not set it: "from Host *", Settings ▸ SSH Options,
        "OpenSSH default". Empty with no answer."""
        key = canonical_keyword(keyword)
        if not self.available or key not in self.values:
            return ""
        return messages.source_label(
            self.sources.get(key), forced=key in self.forced, default=key in self.defaults,
        )

    def inherited(self, keyword: str) -> Tuple[str, str]:
        """``(value, where it comes from)`` for a field that does not set
        *keyword*. Empty strings when there is no answer."""
        value = self.used(keyword)
        if not value:
            return "", ""
        return value, self.source_text(keyword)

    def source_note(self, keyword: str) -> str:
        """The value of *keyword* and where it comes from, as a sentence
        ("OpenSSH uses shared from line 5 (Host *)", "OpenSSH default: 22").
        Empty with nothing to say."""
        key = canonical_keyword(keyword)
        value = self.used(key)
        if not value:
            return ""
        return messages.inherited_text(
            value, self.sources.get(key),
            forced=key in self.forced, default=key in self.defaults,
        )

    # -- lines -------------------------------------------------------------

    def verdict(self, line: int, keyword: str = "") -> LineVerdict:
        """The mark of option line *line* (of the evaluated text), whose
        keyword is *keyword* (needed to say the value used instead)."""
        error = self.errors.get(line) if line else None
        if error is not None:
            text = messages.error_text(error)
            return LineVerdict(REJECTED, _("Rejected"), "error", text, text)
        if not self.available or not line:
            return NOTHING
        item = self.states.get(line)
        state = getattr(item, "state", None)
        value = self.used(keyword) if keyword else ""
        if state is State.SHADOWED:
            place = messages.where(item.decided_file, item.decided_line, item.decided_block)
            tooltip = messages.state_tooltip(item)
            target = None
            if item.decided_line:
                target = (item.decided_file or "", item.decided_line)
                tooltip += "\n" + messages.show_line_hint()
            return LineVerdict(
                NO_EFFECT, _("No effect"), messages.STATE_STYLES[State.SHADOWED],
                tooltip,
                messages.used_instead_text(value, place) if value else "",
                messages.used_instead_markup(value, place) if value else "",
                target,
            )
        if state is State.FORCED:
            return LineVerdict(
                OVERRIDDEN, messages.state_label(State.FORCED),
                messages.STATE_STYLES[State.FORCED], messages.state_tooltip(item),
                messages.used_instead_text(value, "", forced=True) if value else "",
                messages.used_instead_markup(value, "", forced=True) if value else "",
            )
        return LineVerdict(USED)
