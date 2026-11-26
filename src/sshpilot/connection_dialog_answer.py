"""OpenSSH's answer for the connection being edited, as the editor reads it.

The editor asks one question per edit: ``connections.evaluate_connection_edit``
with what a save would send. The daemon writes the Host block with the
save's own code, evaluates that file as Advanced Configuration evaluates a
draft, and answers with the values in force, where each comes from, and the
state of each of the host's own lines. Every SSH option the editor shows
takes its value, its source and its marks from this one answer, worded by
:mod:`sshpilot.gtk.advanced_config_messages` as Advanced Configuration
words them.

No GTK here: the rules are tested headless.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .api.models.connections import (
    ConnectionEditEvaluation,
    SshConfigDraftLineState as State,
)
from .connection_dialog_auth import canonical_keyword
from .gtk import advanced_config_messages as messages

#: How an own line of the host's block fares.
USED = "used"            # OpenSSH takes its value (or collects it)
NO_EFFECT = "no_effect"  # an earlier line set the option first
OVERRIDDEN = "overridden"  # Settings ▸ SSH Options decide the option
REJECTED = "rejected"    # OpenSSH refuses the line


class EditorAnswer:
    """One :class:`ConnectionEditEvaluation`, read for the editor's fields.

    Keywords are compared as OpenSSH compares them: lower case, and either
    spelling of a renamed keyword is one setting.
    """

    def __init__(self, result: ConnectionEditEvaluation) -> None:
        self.result = result
        evaluation = result.evaluation
        #: OpenSSH evaluated the edit; otherwise nothing here is known.
        self.available = bool(evaluation is not None and evaluation.evaluated)
        self.refusal = result.refusal
        self.host = result.host
        self.values: Dict[str, List[str]] = {}
        self.sources: Dict[str, object] = {}
        self.defaults: set = set()
        self.forced: Dict[str, str] = {}
        self.states: Dict[int, object] = {}
        self.own: Dict[str, List[Tuple[int, str]]] = {}
        self.inherited_values: Dict[str, List[str]] = {}
        #: Lines of the rendered file OpenSSH rejects, by line number.
        self.errors: Dict[int, object] = {
            error.line: error
            for error in (getattr(evaluation, "errors", ()) or ())
            if not error.file and error.line
        }
        for item in result.own_lines:
            self.own.setdefault(canonical_keyword(item.key), []).append(
                (item.line, item.value)
            )
        if not self.available:
            return
        for item in evaluation.values:
            self.values.setdefault(canonical_keyword(item.key), []).append(item.value)
        self.sources = {canonical_keyword(s.key): s for s in evaluation.sources}
        self.defaults = {canonical_keyword(key) for key in evaluation.defaults}
        for item in evaluation.forced:
            self.forced.setdefault(canonical_keyword(item.key), item.value)
        self.states = {item.line: item for item in evaluation.lines}
        without = result.without_own
        if without is not None and without.evaluated:
            for item in without.values:
                self.inherited_values.setdefault(
                    canonical_keyword(item.key), []
                ).append(item.value)

    # -- values ------------------------------------------------------------

    def first(self) -> Dict[str, str]:
        """Keyword -> the value in force (the first OpenSSH obtained)."""
        return {key: values[0] for key, values in self.values.items() if values}

    def inherited_first(self) -> Dict[str, str]:
        """As :meth:`first`, for the second question (without the host's own
        lines for the keywords asked about)."""
        return {
            key: values[0] for key, values in self.inherited_values.items() if values
        }

    def used(self, keyword: str) -> str:
        """The value OpenSSH uses for *keyword*, every value joined for an
        option it collects; empty when there is none or no answer."""
        return ", ".join(self.values.get(canonical_keyword(keyword), ()))

    def sets(self, keyword: str) -> bool:
        """The host's own block has a line for *keyword*."""
        return bool(self.own.get(canonical_keyword(keyword)))

    def own_value(self, keyword: str) -> Optional[str]:
        """The value of the host's first own line for *keyword*, or None."""
        lines = self.own.get(canonical_keyword(keyword))
        return lines[0][1] if lines else None

    # -- where a value comes from -------------------------------------------

    def source_text(self, keyword: str) -> str:
        """Where a value the host does not set comes from: "from Host *",
        Settings ▸ SSH Options, "OpenSSH default". Empty with no answer."""
        key = canonical_keyword(keyword)
        if not self.available or key not in self.values:
            return ""
        return messages.source_label(
            self.sources.get(key),
            forced=key in self.forced, default=key in self.defaults,
        )

    def is_default(self, keyword: str) -> bool:
        return canonical_keyword(keyword) in self.defaults

    def inherited_note(self, keyword: str) -> str:
        """The note on a field the host leaves empty: what OpenSSH uses
        instead and where it comes from. Empty with nothing to say."""
        key = canonical_keyword(keyword)
        value = self.used(key)
        if not value or self.sets(key):
            return ""
        return messages.inherited_text(
            value, self.sources.get(key),
            forced=key in self.forced, default=key in self.defaults,
        )

    # -- the host's own lines ----------------------------------------------

    def line_state(self, keyword: str) -> Tuple[Optional[str], str]:
        """``(state, note)`` of the host's own line for *keyword*.

        The state is one of :data:`USED`, :data:`NO_EFFECT`,
        :data:`OVERRIDDEN`, :data:`REJECTED`, or None when the block has no
        line for it (or there is no answer). The note says, as Advanced
        Configuration says it, what OpenSSH takes instead and from where.
        """
        lines = self.own.get(canonical_keyword(keyword))
        if not lines:
            return None, ""
        return self._judge(keyword, lines[0][0])

    def _judge(self, keyword: str, line: int) -> Tuple[Optional[str], str]:
        """``(state, note)`` of one own line, by its line number."""
        error = self.errors.get(line)
        if error is not None:
            return REJECTED, messages.error_text(error)
        if not self.available:
            return None, ""
        item = self.states.get(line)
        state = getattr(item, "state", None)
        if state is State.SHADOWED:
            place = messages.where(item.decided_file, item.decided_line, item.decided_block)
            return NO_EFFECT, messages.used_instead_text(self.used(keyword), place)
        if state is State.FORCED:
            return OVERRIDDEN, messages.used_instead_text(
                self.used(keyword), "", forced=True
            )
        return USED, ""

    def line_state_of(self, keyword: str, value: str) -> Tuple[Optional[str], str]:
        """As :meth:`line_state`, for the host's own line that sets *keyword*
        to *value* (an Extra Options row): the first line when several
        do. ``(None, "")`` when no line does."""
        key = canonical_keyword(keyword)
        wanted = " ".join(str(value).split())
        for line, own_value in self.own.get(key, ()):
            if " ".join(own_value.split()) == wanted:
                return self._judge(key, line)
        return None, ""

    def unused_count(self) -> Tuple[int, int]:
        """``(overridden by Settings, no effect)``: own lines not used."""
        forced = shadowed = 0
        for lines in self.own.values():
            for line, _value in lines:
                state = getattr(self.states.get(line), "state", None)
                if state is State.FORCED:
                    forced += 1
                elif state is State.SHADOWED:
                    shadowed += 1
        return forced, shadowed

    def inherited_options(self, keywords) -> List[Tuple[str, str, str]]:
        """``(keyword, value, where)`` of each option in *keywords* (as
        spelled there) that the host does not set and that another block,
        another file or Settings decide: what the Extra Options page lists
        as inherited. OpenSSH's defaults are not listed."""
        found = []
        for keyword in keywords:
            key = canonical_keyword(keyword)
            if self.sets(key) or key in self.defaults or key not in self.values:
                continue
            source = self.sources.get(key)
            if key in self.forced:
                where = messages.forced_source()
            elif source is not None:
                where = messages.where(source.file, source.line, source.block)
            else:
                continue
            found.append((keyword, self.used(key), where))
        return found
