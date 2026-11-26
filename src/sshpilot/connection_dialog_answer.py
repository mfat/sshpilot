"""OpenSSH's answer for the connection being edited, as the editor reads it.

The editor asks one question per edit: ``connections.evaluate_connection_edit``
with what a save would send. The daemon writes the Host block with the
save's own code, evaluates that file as Advanced Configuration evaluates a
draft, and answers with the values in force, where each comes from, and the
state of each of the host's own lines.

It is read by :class:`sshpilot.ssh_config_answer.ConfigAnswer`, the reader
Advanced Configuration uses, and each line's mark is that reader's
:class:`~sshpilot.ssh_config_answer.LineVerdict`. This module only adds
what is the editor's own: finding the host's lines by keyword, the second
question (without the host's own login lines), and the inherited options
the Extra Options page lists.

No GTK here: the rules are tested headless.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .api.models.connections import ConnectionEditEvaluation
from .connection_dialog_auth import canonical_keyword
from .gtk import advanced_config_messages as messages
from .ssh_config_answer import (  # noqa: F401 - the states, for the editor
    NO_EFFECT,
    NOTHING,
    OVERRIDDEN,
    REJECTED,
    USED,
    ConfigAnswer,
    LineVerdict,
)


class EditorAnswer(ConfigAnswer):
    """One :class:`ConnectionEditEvaluation`, read for the editor's fields."""

    def __init__(self, result: ConnectionEditEvaluation) -> None:
        super().__init__(result.evaluation)
        self.result = result
        self.refusal = result.refusal
        self.host = result.host
        self.own: Dict[str, List[Tuple[int, str]]] = {}
        self.inherited_values: Dict[str, List[str]] = {}
        for item in result.own_lines:
            self.own.setdefault(canonical_keyword(item.key), []).append(
                (item.line, item.value)
            )
        without = result.without_own
        if self.available and without is not None and without.evaluated:
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

    def sets(self, keyword: str) -> bool:
        """The host's own block has a line for *keyword*."""
        return bool(self.own.get(canonical_keyword(keyword)))

    def own_value(self, keyword: str) -> Optional[str]:
        """The value of the host's first own line for *keyword*, or None."""
        lines = self.own.get(canonical_keyword(keyword))
        return lines[0][1] if lines else None

    def inherited_note(self, keyword: str) -> str:
        """The note on a field the host leaves empty: what OpenSSH uses
        instead and where it comes from. Empty with nothing to say."""
        if self.sets(keyword):
            return ""
        return self.source_note(keyword)

    # -- the host's own lines ----------------------------------------------

    def line_verdict(self, keyword: str) -> LineVerdict:
        """The mark of the host's own line for *keyword* (its first, the
        one that counts): :data:`NOTHING` when the block has none.

        A tag that links to the winning line names its file: the edited
        connection's own file when the line is in it."""
        lines = self.own.get(canonical_keyword(keyword))
        if not lines:
            return NOTHING
        return self._located(self.verdict(lines[0][0], keyword))

    def line_verdict_of(self, keyword: str, value: str) -> LineVerdict:
        """As :meth:`line_verdict`, for the host's own line that sets
        *keyword* to *value* (an Extra Options row): the first line when
        several do. :data:`NOTHING` when no line does."""
        key = canonical_keyword(keyword)
        wanted = " ".join(str(value).split())
        for line, own_value in self.own.get(key, ()):
            if " ".join(own_value.split()) == wanted:
                return self._located(self.verdict(line, key))
        return NOTHING

    def _located(self, verdict: LineVerdict) -> LineVerdict:
        if verdict.target is None or verdict.target[0]:
            return verdict
        from dataclasses import replace

        return replace(verdict, target=(self.result.file or "", verdict.target[1]))

    def line_state(self, keyword: str) -> Tuple[Optional[str], str]:
        """``(state, note)`` of the host's own line for *keyword*."""
        verdict = self.line_verdict(keyword)
        return verdict.state, verdict.note

    def line_state_of(self, keyword: str, value: str) -> Tuple[Optional[str], str]:
        verdict = self.line_verdict_of(keyword, value)
        return verdict.state, verdict.note

    def unused_count(self) -> Tuple[int, int]:
        """``(overridden by Settings, no effect)``: own lines not used."""
        forced = shadowed = 0
        for lines in self.own.values():
            for line, _value in lines:
                state = self.verdict(line).state
                if state == OVERRIDDEN:
                    forced += 1
                elif state == NO_EFFECT:
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
