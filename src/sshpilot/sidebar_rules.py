"""The rules of an SSH configuration, read for the sidebar's Rules view.

A connection is a ``Host`` block that names a host. Everything else in the
configuration sets options for hosts it does not name: the options above
every block, a ``Host`` line with a pattern, a ``Match`` block. Those are the
rules, listed here in file order, which is the order OpenSSH applies them in.

GTK-free: ``sidebar_views`` draws what this returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .ssh_config_blocks import GLOBAL, HOST, MATCH, scan_blocks
from .ssh_config_document import is_literal_host_token

#: How many option names a row lists before it trails off.
SUMMARY_KEYWORDS = 4


@dataclass(frozen=True)
class Rule:
    kind: str               # GLOBAL, HOST or MATCH
    line: int               # header line in its file; the global options' first line
    patterns: str           # what follows Host or Match; "" for the global options
    keywords: Tuple[str, ...]   # the options it sets, as written, each once
    file: str = ""          # the daemon's name for the file; "" is the root
    file_label: str = ""    # shown for a file other than the root


def _written_keyword(text: str) -> str:
    word = text.split("=", 1)[0].split(None, 1)[0] if text.strip() else ""
    return word.strip('"')


def read_rules(text: str, file: str = "", file_label: str = "") -> List[Rule]:
    """The rules of one file's *text*, in file order."""
    rules: List[Rule] = []
    for block in scan_blocks(text):
        if block.kind == HOST:
            tokens = block.patterns.split()
            if tokens and all(is_literal_host_token(token) for token in tokens):
                continue                # a connection, or several: not a rule
        elif block.kind not in (GLOBAL, MATCH):
            continue
        keywords: List[str] = []
        seen = set()
        for line in block.lines:
            if line.keyword in seen:
                continue
            seen.add(line.keyword)
            keywords.append(_written_keyword(line.text) or line.keyword)
        rules.append(Rule(
            kind=block.kind,
            # The global options have no header: their first line stands for them.
            line=block.line or (block.lines[0].line if block.lines else 0),
            patterns=block.patterns.strip(),
            keywords=tuple(keywords), file=file, file_label=file_label,
        ))
    return rules


def summary(rule: Rule) -> str:
    """The option names a rule sets, the first few of them."""
    shown = ", ".join(rule.keywords[:SUMMARY_KEYWORDS])
    if len(rule.keywords) > SUMMARY_KEYWORDS:
        shown += ", …"
    return shown


def matches(rule: Rule, query: str) -> bool:
    """Whether *rule* is kept by what is typed in the search field."""
    words = (query or "").casefold().split()
    if not words:
        return True
    haystack = " ".join(
        (rule.kind, rule.patterns, rule.file_label, *rule.keywords)
    ).casefold()
    return all(word in haystack for word in words)
