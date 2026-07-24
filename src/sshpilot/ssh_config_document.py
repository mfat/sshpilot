"""Lossless per-file SSH config document model.

Parses one ssh_config file into an ordered list of nodes that keep every
byte of the source: ``HostBlock`` for editable ``Host`` stanzas, ``RawSpan``
for everything else (comments, blank lines, Match blocks, Include lines,
global directives). ``text()`` re-serializes byte-for-byte; that invariant is
asserted at parse time.

Block boundaries follow the long-standing scanner rule used across
connection_manager: a block starts at a line whose keyword is ``Host``
(any ssh_config(5) separator form) and runs until the next
``Host``/``Match``/``Include`` header — trailing comments/blank lines before
the next header belong to the preceding block's span.

``Host`` blocks are modeled because they are the only thing the app edits;
``Match`` blocks are modeled (lines only, never edited) because the loader
collects them as preservation rules. Everything else stays ``RawSpan``.
Multi-file resolution (Include) stays in ``ssh_config_utils`` — a document
is always a single file.
"""

import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

# Per ssh_config(5): "Configuration options may be separated by whitespace or
# optional whitespace and exactly one '='". A keyword and its argument may be
# separated by any run of whitespace (spaces or tabs) or by a single '=' with
# optional surrounding whitespace.
_CONFIG_OPTION_RE = re.compile(r'^(\S+?)(?:\s*=\s*|\s+)(.*)$')


def _split_config_option(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a config line into (key, value) honouring whitespace and '=' separators.

    Returns ``(None, None)`` for lines that carry no value (a bare keyword).
    """
    match = _CONFIG_OPTION_RE.match(line)
    if not match:
        return None, None
    key, value = match.group(1), match.group(2).strip()
    if not value:
        return None, None
    return key, value


def _split_keyword(line: str) -> Tuple[str, str]:
    """Return ``(lowercased keyword, remainder)`` for a config line.

    Honours every ssh_config(5) separator, so ``Host x``, ``Host=x``,
    ``Host = x`` and tab-separated forms all yield ``('host', 'x')``. A bare
    keyword with no argument yields ``('host', '')``.
    """
    match = _CONFIG_OPTION_RE.match(line)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    return line.strip().lower(), ''


def split_host_tokens(remainder: str) -> List[str]:
    """Tokenize a Host header's argument, falling back to whitespace split
    when quoting is unbalanced (the same tolerance the scanners had)."""
    if not remainder:
        return []
    try:
        return shlex.split(remainder)
    except ValueError:
        return [t for t in remainder.split() if t]


_BLOCK_HEADERS = ('host', 'match', 'include')


@dataclass
class HostBlock:
    """One editable ``Host`` stanza, lines kept verbatim (header included)."""
    tokens: List[str]
    lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        return ''.join(self.lines)


@dataclass
class MatchBlock:
    """One ``Match`` stanza, lines verbatim. Never edited; the loader keeps
    these as preservation rules."""
    lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        return ''.join(self.lines)


@dataclass
class RawSpan:
    """Any run of lines the app never edits — preserved byte-for-byte."""
    lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        return ''.join(self.lines)


Node = Union[HostBlock, MatchBlock, RawSpan]


class SSHConfigDocument:
    """Ordered, lossless view of one ssh_config file."""

    def __init__(self, nodes: List[Node], path: Optional[str] = None,
                 newline: str = '\n'):
        self.nodes = nodes
        self.path = path
        # The file's line-ending style; generated lines are converted to it
        # via render_lines() so an edit never mixes endings.
        self.newline = newline

    @classmethod
    def parse_text(cls, text: str, path: Optional[str] = None) -> 'SSHConfigDocument':
        lines = text.splitlines(keepends=True)
        nodes: List[Node] = []
        raw: List[str] = []

        def flush_raw():
            if raw:
                nodes.append(RawSpan(lines=list(raw)))
                raw.clear()

        i = 0
        while i < len(lines):
            keyword, remainder = _split_keyword(lines[i].lstrip())
            if keyword in ('host', 'match'):
                flush_raw()
                if keyword == 'host':
                    block: Node = HostBlock(tokens=split_host_tokens(remainder),
                                            lines=[lines[i]])
                else:
                    block = MatchBlock(lines=[lines[i]])
                i += 1
                while i < len(lines) and \
                        _split_keyword(lines[i].strip())[0] not in _BLOCK_HEADERS:
                    block.lines.append(lines[i])
                    i += 1
                nodes.append(block)
                continue
            raw.append(lines[i])
            i += 1
        flush_raw()

        doc = cls(nodes, path=path,
                  newline='\r\n' if '\r\n' in text else '\n')
        # Losslessness is the whole point — fail loudly, not subtly.
        assert doc.text() == text, "SSHConfigDocument lost bytes while parsing"
        return doc

    @classmethod
    def parse_file(cls, path: str) -> 'SSHConfigDocument':
        # newline='' disables universal-newline translation so CRLF configs
        # are seen (and re-serialized) byte-for-byte.
        with open(path, encoding='utf-8', newline='') as f:
            return cls.parse_text(f.read(), path=path)

    def text(self) -> str:
        return ''.join(node.text() for node in self.nodes)

    def render_lines(self, lines: List[str]) -> List[str]:
        """Convert lines to this document's newline style so edits never mix
        endings. Idempotent: input may mix generated LF lines with preserved
        lines that already carry the document's CRLF (a bare replace would
        double-convert those to CR CR LF)."""
        if self.newline == '\n':
            return list(lines)
        return [line.replace('\r\n', '\n').replace('\n', self.newline)
                for line in lines]

    def host_blocks(self, token: Optional[str] = None) -> List[HostBlock]:
        """All Host blocks, or only those whose token list contains *token*
        (exact membership — no pattern matching, same as the scanners)."""
        blocks = [n for n in self.nodes if isinstance(n, HostBlock)]
        if token is None:
            return blocks
        return [b for b in blocks if token in b.tokens]
