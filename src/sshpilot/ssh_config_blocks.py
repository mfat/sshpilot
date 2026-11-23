"""An SSH configuration as an ordered list of editable blocks (GTK-free).

Advanced Configuration shows a configuration the way OpenSSH reads it: the
options above every ``Host``/``Match`` line (the *global* block), then each
``Host`` and ``Match`` block in file order. This module turns text into that
list and back.

The round trip is lossless: every line is kept as written, and a line is
rewritten only when its keyword, value or header was edited. Comment and
blank lines stay attached to the line that follows them, so they travel with
it when a block or an option is moved.

A block can be switched off without losing it: its lines are written as
comments that start with :data:`DISABLED_PREFIX`, which OpenSSH ignores and
this parser reads back as a disabled block.

The daemon uses the same parser (:func:`scan_blocks`) to locate blocks in a
draft, so line numbers mean the same thing on both sides.
"""
from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .ssh_config_document import _split_keyword, split_config_lines

GLOBAL = "global"
HOST = "host"
MATCH = "match"

#: Marks a line of a block that is switched off.
DISABLED_PREFIX = "# [disabled] "
#: Written for a switched-off global block, which has no header of its own.
DISABLED_GLOBAL = "# [disabled global section]"

_INDENT = "    "
_ids = itertools.count(1)


def _next_id() -> int:
    return next(_ids)


def split_inline_comment(remainder: str) -> Tuple[str, str]:
    """``(value, comment)``: a ``#`` after white space, outside quotes, starts
    a comment (OpenSSH 8.5 and later; older versions take it as the value)."""
    in_quotes = False
    for index, char in enumerate(remainder):
        if char == '"':
            in_quotes = not in_quotes
        elif char == "#" and not in_quotes and (index == 0 or remainder[index - 1].isspace()):
            return remainder[:index].rstrip(), remainder[index:].strip()
    return remainder.strip(), ""


@dataclass
class Option:
    """One option line of a block."""

    key: str = ""
    value: str = ""
    comment: str = ""                      # inline "# ..." after the value
    leading: List[str] = field(default_factory=list)   # comment/blank lines above
    raw: Optional[str] = None              # the line as written, if unedited
    uid: int = field(default_factory=_next_id)
    _parsed: Tuple[str, str] = ("", "")

    @property
    def keyword(self) -> str:
        return self.key.strip().lower()

    def line_text(self, indent: str, disabled: bool) -> str:
        if (
            self.raw is not None
            and (self.key, self.value) == self._parsed
            and self.raw.strip().startswith(DISABLED_PREFIX) == disabled
        ):
            return self.raw
        text = f"{indent}{self.key.strip()} {self.value.strip()}".rstrip()
        if self.comment:
            text += f" {self.comment}"
        return (DISABLED_PREFIX + text) if disabled else text


@dataclass
class Block:
    """The global options, or one ``Host``/``Match`` block."""

    kind: str = HOST
    patterns: str = ""
    comment: str = ""                      # inline comment on the header
    options: List[Option] = field(default_factory=list)
    leading: List[str] = field(default_factory=list)
    disabled: bool = False
    raw: Optional[str] = None
    uid: int = field(default_factory=_next_id)
    _parsed: Tuple[str, bool] = ("", False)

    def header_text(self) -> Optional[str]:
        if self.kind == GLOBAL:
            return DISABLED_GLOBAL if self.disabled else None
        if self.raw is not None and (self.patterns, self.disabled) == self._parsed:
            return self.raw
        default = "*" if self.kind == HOST else "all"
        text = f"{'Host' if self.kind == HOST else 'Match'} {self.patterns.strip() or default}"
        if self.comment:
            text += f" {self.comment}"
        return (DISABLED_PREFIX + text) if self.disabled else text

    # -- the note: the comment lines directly above the header --------------

    def _note_span(self) -> Tuple[int, int]:
        end = len(self.leading)
        start = end
        while start > 0 and self.leading[start - 1].strip().startswith("#"):
            start -= 1
        return start, end

    @property
    def note(self) -> str:
        start, end = self._note_span()
        return "\n".join(
            line.strip()[1:].strip() for line in self.leading[start:end]
        )

    @note.setter
    def note(self, text: str) -> None:
        start, end = self._note_span()
        lines = [f"# {part.strip()}" for part in (text or "").splitlines() if part.strip()]
        self.leading[start:end] = lines


@dataclass
class ConfigDraft:
    """A configuration being edited."""

    blocks: List[Block] = field(default_factory=list)
    trailing: List[str] = field(default_factory=list)
    newline: str = "\n"
    final_newline: bool = True

    def clone(self) -> "ConfigDraft":
        """A deep copy that keeps every uid (for undo)."""
        return copy.deepcopy(self)

    def find_block(self, uid: int) -> Optional[Block]:
        return next((block for block in self.blocks if block.uid == uid), None)

    def find_option(self, uid: int) -> Tuple[Optional[Block], Optional[Option]]:
        for block in self.blocks:
            for option in block.options:
                if option.uid == uid:
                    return block, option
        return None, None


@dataclass(frozen=True)
class LineMap:
    """Where :func:`serialize` put each block and option (1-based lines)."""

    headers: Dict[int, int]     # block uid -> header line (0: none)
    options: Dict[int, int]     # option uid -> line


def _line_parts(line: str) -> Tuple[bool, str, str, str]:
    """``(disabled, keyword, value, comment)`` of an option or header line;
    the keyword is empty for a blank line or an ordinary comment."""
    stripped = line.strip()
    disabled = stripped.startswith(DISABLED_PREFIX)
    if disabled:
        stripped = stripped[len(DISABLED_PREFIX):].strip()
    if not stripped or stripped.startswith("#"):
        return disabled, "", "", ""
    keyword, remainder = _split_keyword(stripped)
    written = stripped[: len(keyword)]
    value, comment = split_inline_comment(remainder)
    return disabled, written, value, comment


def parse(text: str) -> ConfigDraft:
    """Read configuration text into blocks."""
    text = text or ""
    if text.startswith("﻿"):
        text = text[1:]
    draft = ConfigDraft(
        newline="\r\n" if "\r\n" in text else "\n",
        final_newline=text.endswith(("\n", "\r")) or not text,
    )
    current: Optional[Block] = None
    pending: List[str] = []
    for line in split_config_lines(text):
        if line.strip() == DISABLED_GLOBAL and current is None:
            current = Block(kind=GLOBAL, disabled=True, leading=pending, raw=line)
            current._parsed = ("", True)
            draft.blocks.append(current)
            pending = []
            continue
        disabled, key, value, comment = _line_parts(line)
        if not key or (disabled and key.lower() not in (HOST, MATCH)
                       and not (current is not None and current.disabled)):
            # Blank, an ordinary comment, or a "[disabled]" line that does
            # not belong to a switched-off block: kept as written.
            pending.append(line)
            continue
        if key.lower() in (HOST, MATCH):
            current = Block(
                kind=key.lower(), patterns=value, comment=comment,
                leading=pending, disabled=disabled, raw=line,
            )
            current._parsed = (value, disabled)
            draft.blocks.append(current)
            pending = []
            continue
        if current is None or (current.disabled and not disabled):
            # OpenSSH does not see a switched-off block, so an active line
            # after one belongs to the last block that is on (or to the
            # global options when there is none).
            current = next((b for b in reversed(draft.blocks) if not b.disabled), None)
            if current is None:
                current = Block(kind=GLOBAL)
                draft.blocks.append(current)
        option = Option(key=key, value=value, comment=comment, leading=pending, raw=line)
        option._parsed = (key, value)
        current.options.append(option)
        pending = []
    draft.trailing = pending
    return draft


def serialize(draft: ConfigDraft) -> Tuple[str, LineMap]:
    """Write *draft* as text, with the line of every block and option."""
    lines: List[str] = []
    headers: Dict[int, int] = {}
    options: Dict[int, int] = {}
    for index, block in enumerate(draft.blocks):
        # A block made in the editor has no blank line above it yet.
        if index and block.raw is None and block.kind != GLOBAL and not (
            block.leading and not block.leading[0].strip()
        ) and lines and lines[-1].strip():
            lines.append("")
        lines.extend(block.leading)
        header = block.header_text()
        if header is not None:
            lines.append(header)
            headers[block.uid] = len(lines)
        else:
            headers[block.uid] = 0
        indent = "" if block.kind == GLOBAL else _INDENT
        for option in block.options:
            if not option.key.strip():
                continue  # a row still being typed
            lines.extend(option.leading)
            lines.append(option.line_text(indent, block.disabled))
            options[option.uid] = len(lines)
    lines.extend(draft.trailing)
    text = draft.newline.join(lines)
    if lines and draft.final_newline:
        text += draft.newline
    return text, LineMap(headers, options)


# -- the daemon's view ---------------------------------------------------------

@dataclass(frozen=True)
class ScannedLine:
    line: int
    keyword: str        # lower case
    value: str
    text: str           # the stripped line


@dataclass(frozen=True)
class ScannedBlock:
    kind: str
    line: int           # header line; 0 for the global block
    patterns: str
    lines: Tuple[ScannedLine, ...]


def scan_blocks(text: str) -> Tuple[ScannedBlock, ...]:
    """The active blocks of *text*, as OpenSSH reads them, with line numbers.

    Comments (switched-off blocks included) and blank lines are not part of
    any block. The global block is present only when it has lines.
    """
    blocks: List[List] = []
    current: Optional[List] = None
    for number, raw in enumerate(split_config_lines(text or ""), start=1):
        stripped = raw.strip()
        if number == 1 and stripped.startswith("﻿"):
            stripped = stripped[1:].strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, remainder = _split_keyword(stripped)
        value, _comment = split_inline_comment(remainder)
        if keyword in (HOST, MATCH):
            current = [keyword, number, value, []]
            blocks.append(current)
            continue
        if current is None:
            current = [GLOBAL, 0, "", []]
            blocks.append(current)
        current[3].append(ScannedLine(number, keyword, value, stripped))
    return tuple(
        ScannedBlock(kind, line, patterns, tuple(lines))
        for kind, line, patterns, lines in blocks
    )
