"""An SSH configuration as an ordered list of editable blocks (GTK-free).

Advanced Configuration shows a configuration the way OpenSSH reads it: the
options above every ``Host``/``Match`` line (the *global* block), then each
``Host`` and ``Match`` block in file order. This module turns text into that
list and back.

**The round trip is exact.** Text that was not edited is written back byte
for byte: every line is kept with its own line ending, a byte-order mark is
kept, and a line is rewritten only when its keyword, value or header was
edited. An edited line keeps its indentation, the way its keyword was
written and its separator. Comment and blank lines stay attached to the
line that follows them, so they travel with it when a block or an option is
moved.

A block can be switched off without losing it: its lines are written as
comments that start with :data:`DISABLED_PREFIX`, which OpenSSH ignores and
this parser reads back as a disabled block. Such comments are read as a
block only when that changes nothing: if an active option follows them
before the next header, OpenSSH reads that option into the block above, and
turning the comments into a block would move it. They are then left as the
comments they are.

The daemon uses the same reading (:func:`scan_blocks`) to locate blocks in a
draft, so line numbers mean the same thing on both sides.
"""
from __future__ import annotations

import copy
import itertools
import re
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
_BOM = "﻿"
_ids = itertools.count(1)
# indentation, keyword (possibly quoted), separator, the rest
_LINE_RE = re.compile(r'^(\s*)("[^"]*"|[^\s=]+)(\s*=\s*|\s+|$)(.*)$', re.DOTALL)


def _next_id() -> int:
    return next(_ids)


def _eol(line: str) -> str:
    """The line terminator *line* ends with ('' for an unterminated one)."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith(("\n", "\r")):
        return line[-1]
    return ""


def _body(line: str) -> str:
    return line[: len(line) - len(_eol(line))]


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


def keyword_of(line: str) -> str:
    """The lower-case keyword of an active line, as OpenSSH reads it: quotes
    around it do not count. Empty for a blank line or a comment."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    match = _LINE_RE.match(stripped)
    written = match.group(2) if match else _split_keyword(stripped)[0]
    return written.strip('"').lower()


@dataclass
class _Written:
    """How a line was written: kept when the line is edited."""

    indent: str = ""
    keyword: str = ""       # as spelled (and quoted)
    separator: str = " "
    eol: str = ""


def _parts(line: str) -> Tuple[bool, _Written, str, str]:
    """``(disabled, how it is written, value, comment)`` of a line. The
    written keyword is empty for a blank line or an ordinary comment."""
    eol = _eol(line)
    text = _body(line)
    stripped = text.strip()
    disabled = stripped.startswith(DISABLED_PREFIX)
    if disabled:
        outer = text[: len(text) - len(text.lstrip())]
        text = outer + stripped[len(DISABLED_PREFIX):]
        stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return disabled, _Written(eol=eol), "", ""
    match = _LINE_RE.match(text)
    if match is None:
        return disabled, _Written(eol=eol), "", ""
    indent, keyword, separator, rest = match.groups()
    value, comment = split_inline_comment(rest)
    return disabled, _Written(indent, keyword, separator or " ", eol), value, comment


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
    _written: Optional[_Written] = None
    _was_disabled: bool = False

    @property
    def keyword(self) -> str:
        return self.key.strip().strip('"').lower()

    def line_text(self, indent: str, disabled: bool, eol: str) -> str:
        if (
            self.raw is not None
            and (self.key, self.value) == self._parsed
            and self._was_disabled == disabled
        ):
            return self.raw
        written = self._written
        key = self.key.strip()
        if written is not None:
            indent = written.indent
            separator = written.separator
            eol = written.eol         # '' for an unterminated last line
        else:
            separator = " "
        text = f"{indent}{key}{separator}{self.value.strip()}".rstrip()
        if self.comment:
            text += f" {self.comment}"
        return ((DISABLED_PREFIX + text) if disabled else text) + eol


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
    _written: Optional[_Written] = None

    def header_text(self, eol: str) -> Optional[str]:
        if self.kind == GLOBAL:
            if not self.disabled:
                return None
            return self.raw if self.raw is not None else DISABLED_GLOBAL + eol
        if self.raw is not None and (self.patterns, self.disabled) == self._parsed:
            return self.raw
        default = "*" if self.kind == HOST else "all"
        written = self._written
        keyword = written.keyword if written else ("Host" if self.kind == HOST else "Match")
        indent = written.indent if written else ""
        separator = written.separator if written else " "
        text = f"{indent}{keyword}{separator}{self.patterns.strip() or default}"
        if self.comment:
            text += f" {self.comment}"
        return ((DISABLED_PREFIX + text) if self.disabled else text) + (
            (written.eol if written else "") or eol
        )

    # -- the note: the comment lines directly above the header --------------
    # (The global block has no header to put a note above: it has none.)

    def _note_span(self) -> Tuple[int, int]:
        end = len(self.leading)
        start = end
        while start > 0 and self.leading[start - 1].strip().startswith("#"):
            start -= 1
        return start, end

    @property
    def note(self) -> str:
        if self.kind == GLOBAL:
            return ""
        start, end = self._note_span()
        return "\n".join(
            line.strip()[1:].strip() for line in self.leading[start:end]
        )

    def set_note(self, text: str, eol: str = "\n") -> None:
        if self.kind == GLOBAL:
            return
        start, end = self._note_span()
        old = self.leading[start:end]
        ending = (_eol(old[0]) if old else "") or eol
        self.leading[start:end] = [
            f"# {part.strip()}{ending}" for part in (text or "").splitlines() if part.strip()
        ]


@dataclass
class ConfigDraft:
    """A configuration being edited."""

    blocks: List[Block] = field(default_factory=list)
    trailing: List[str] = field(default_factory=list)
    newline: str = "\n"
    bom: bool = False

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


def _self_contained(lines: List[str], start: int) -> bool:
    """Whether the switched-off block whose header is ``lines[start]`` can be
    read as a block: no active option follows it before the next header."""
    for line in lines[start + 1:]:
        disabled, written, _value, _comment = _parts(line)
        if not written.keyword:
            continue
        if written.keyword.strip('"').lower() in (HOST, MATCH):
            return True
        if not disabled:
            return False
    return True


def parse(text: str) -> ConfigDraft:
    """Read configuration text into blocks."""
    text = text or ""
    draft = ConfigDraft(newline="\r\n" if "\r\n" in text else "\n")
    if text.startswith(_BOM):
        draft.bom = True
        text = text[1:]
    lines = split_config_lines(text, keepends=True)
    current: Optional[Block] = None
    seen_active = False
    pending: List[str] = []
    for index, line in enumerate(lines):
        if _body(line).strip() == DISABLED_GLOBAL and not seen_active and not any(
            block.kind == GLOBAL for block in draft.blocks
        ):
            current = Block(kind=GLOBAL, disabled=True, leading=pending, raw=line)
            current._parsed = ("", True)
            draft.blocks.append(current)
            pending = []
            continue
        disabled, written, value, comment = _parts(line)
        keyword = written.keyword.strip('"').lower()
        if not keyword:
            pending.append(line)
            continue
        if keyword in (HOST, MATCH):
            if disabled and not _self_contained(lines, index):
                pending.append(line)          # see the module docstring
                continue
            current = Block(
                kind=keyword, patterns=value, comment=comment,
                leading=pending, disabled=disabled, raw=line,
            )
            current._parsed = (value, disabled)
            current._written = written
            draft.blocks.append(current)
            seen_active = seen_active or not disabled
            pending = []
            continue
        if disabled and not (current is not None and current.disabled):
            pending.append(line)              # an ordinary comment
            continue
        if not disabled and (current is None or current.disabled):
            # Reached only above every active block (a switched-off block
            # followed by an active option is not read as a block).
            current = next(
                (b for b in draft.blocks if b.kind == GLOBAL and not b.disabled), None
            )
            if current is None:
                current = Block(kind=GLOBAL)
                draft.blocks.append(current)
        option = Option(key=written.keyword, value=value, comment=comment,
                        leading=pending, raw=line)
        option._parsed = (written.keyword, value)
        option._written = written
        option._was_disabled = disabled
        current.options.append(option)
        seen_active = seen_active or not disabled
        pending = []
    draft.trailing = pending
    return draft


def serialize(draft: ConfigDraft) -> Tuple[str, LineMap]:
    """Write *draft* as text, with the line of every block and option."""
    eol = draft.newline
    lines: List[str] = []
    headers: Dict[int, int] = {}
    options: Dict[int, int] = {}

    def emit(line: str) -> None:
        # A line that ended the text without a terminator gets one as soon
        # as something follows it.
        if lines and not _eol(lines[-1]):
            lines[-1] += eol
        lines.append(line)

    for index, block in enumerate(draft.blocks):
        # A block made in the editor has no blank line above it yet.
        if (
            index and block.raw is None and block.kind != GLOBAL
            and not (block.leading and not block.leading[0].strip())
            and lines and lines[-1].strip()
        ):
            emit(eol)
        for line in block.leading:
            emit(line)
        header = block.header_text(eol)
        if header is not None:
            emit(header)
            headers[block.uid] = len(lines)
        else:
            headers[block.uid] = 0
        # A new option is indented like the block's own lines.
        indent = "" if block.kind == GLOBAL else next(
            (o._written.indent for o in block.options if o._written is not None), _INDENT
        )
        for option in block.options:
            if not option.key.strip():
                continue  # a row still being typed
            for line in option.leading:
                emit(line)
            emit(option.line_text(indent, block.disabled, eol))
            options[option.uid] = len(lines)
    for line in draft.trailing:
        emit(line)
    return (_BOM if draft.bom else "") + "".join(lines), LineMap(headers, options)


# -- what a block is for --------------------------------------------------------

#: A ``Host`` block of plain names: each one is a host to connect to.
CONNECTION = "connection"
#: A ``Host`` block with a wildcard or a negation: options for what it matches.
PATTERN = "pattern"
#: A ``Match`` block.
MATCH_RULE = "match_rule"
#: Options for every host: those above all blocks, ``Host *``, ``Match all``.
ALL_HOSTS = "all_hosts"


def block_category(kind: str, patterns: str) -> str:
    """Which of the four a block is, from its kind and what follows it.

    A line that names a host beside a pattern (``Host edge *.edge``) is a
    pattern: its options reach more than the host it names. ``Host *`` with
    anything else on the line (``Host * !bastion``) is a pattern too.
    """
    from .ssh_config_document import is_literal_host_token

    if kind == GLOBAL:
        return ALL_HOSTS
    tokens = (patterns or "").split()
    if kind == MATCH:
        return ALL_HOSTS if [t.lower() for t in tokens] == ["all"] else MATCH_RULE
    if tokens == ["*"]:
        return ALL_HOSTS
    if tokens and all(is_literal_host_token(token) for token in tokens):
        return CONNECTION
    return PATTERN


# -- the daemon's view ---------------------------------------------------------

@dataclass(frozen=True)
class ScannedLine:
    line: int
    keyword: str        # lower case, unquoted
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
    text = text or ""
    if text.startswith(_BOM):
        text = text[1:]
    blocks: List[List] = []
    current: Optional[List] = None
    for number, raw in enumerate(split_config_lines(text), start=1):
        stripped = raw.strip()
        keyword = keyword_of(stripped)
        if not keyword:
            continue
        match = _LINE_RE.match(stripped)
        value, _comment = split_inline_comment(match.group(4) if match else "")
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
