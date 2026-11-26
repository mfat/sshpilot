"""Reading OpenSSH's answer about the connection being edited.

The daemon writes the editor's pending Host block with the save's own code
and evaluates the file (``connections.evaluate_connection_edit``). This
module reads what Advanced Configuration's evaluator answers for that text:
the values in force and, for the identity files, which line each comes
from. No GTK here: the rules are tested headless.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# The authentication keywords, as the authentication section knows them.
from .connection_dialog_auth import AUTH_POLICY_KEYWORDS, canonical_keyword
from .ssh_config_blocks import HOST, scan_blocks
from .ssh_config_document import split_host_tokens

_AUTH_KEYWORDS = frozenset(canonical_keyword(k) for k in AUTH_POLICY_KEYWORDS)


def _own_blocks(text: str, host: str):
    """The ``Host`` blocks of *text* that name *host* itself (no pattern)."""
    own = []
    for block in scan_blocks(text):
        if block.kind != HOST:
            continue
        try:
            tokens = split_host_tokens(block.patterns)
        except Exception:
            continue
        if host in tokens:
            own.append(block)
    return own


def own_identity_lines(draft_text: str, host: str) -> Dict[int, str]:
    """Line number -> value of the ``IdentityFile`` lines in *host*'s blocks."""
    return {
        item.line: item.value
        for block in _own_blocks(draft_text, host)
        for item in block.lines
        if item.keyword == "identityfile"
    }


def _same_path(a: str, b: str) -> bool:
    def norm(value: str) -> str:
        value = value.strip().strip('"')
        return os.path.normpath(os.path.expanduser(value)) if value else value

    return norm(a) == norm(b)


@dataclass(frozen=True)
class EffectiveIdentity:
    """One identity file OpenSSH would offer, in its order.

    ``origin`` is ``"host"`` for a line of the host's own block, ``"block"``
    for a line of another block of the root file (``block`` is its header),
    ``"default"`` for OpenSSH's built-in list, and ``""`` when the line that
    supplies it cannot be named (an included file, the system-wide file).
    """

    path: str
    origin: str = ""
    block: str = ""


@dataclass
class DraftAnswer:
    """``ssh -G`` for a draft: keyword (lower case) -> values, in order."""

    values: Dict[str, List[str]] = field(default_factory=dict)
    defaults: frozenset = frozenset()
    identities: Tuple[EffectiveIdentity, ...] = ()

    def first(self) -> Dict[str, str]:
        """Keyword -> the value in force (the first OpenSSH obtained)."""
        return {key: values[0] for key, values in self.values.items() if values}


def read_answer(evaluation, draft_text: str, host: str) -> Optional[DraftAnswer]:
    """An evaluation as a :class:`DraftAnswer`; ``None`` when it has none.

    No answer: OpenSSH rejected the draft, a ``Match exec`` command would
    have to be run first, or nothing came back.
    """
    if evaluation is None:
        return None
    if getattr(evaluation, "errors", None) or getattr(evaluation, "pending_exec", None):
        return None
    pairs = getattr(evaluation, "values", None) or ()
    if not pairs:
        return None
    values: Dict[str, List[str]] = {}
    for pair in pairs:
        values.setdefault(str(pair.key).lower(), []).append(str(pair.value))
    defaults = frozenset(str(k).lower() for k in getattr(evaluation, "defaults", ()) or ())

    # Where each identity file comes from, when a line of the draft says so.
    # OpenSSH lists each file once, in the order it met them.
    applying = {
        int(line.line)
        for line in getattr(evaluation, "lines", ()) or ()
        if getattr(getattr(line, "state", None), "value", "") == "accumulated"
    }
    own = own_identity_lines(draft_text, host)
    candidates: List[Tuple[str, str, str]] = []  # value, origin, block header
    for block in scan_blocks(draft_text):
        header = "" if not block.line else (
            ("Host " if block.kind == HOST else "Match ") + block.patterns
        )
        for item in block.lines:
            if item.keyword != "identityfile" or item.line not in applying:
                continue
            candidates.append(
                (item.value, "host" if item.line in own else "block", header)
            )
    identities: List[EffectiveIdentity] = []
    builtin = "identityfile" in defaults
    for path in values.get("identityfile", ()):
        if builtin:
            identities.append(EffectiveIdentity(path, "default"))
            continue
        source = next((c for c in candidates if _same_path(c[0], path)), None)
        if source is None:
            identities.append(EffectiveIdentity(path))
        else:
            identities.append(EffectiveIdentity(path, source[1], source[2]))
    return DraftAnswer(values=values, defaults=defaults, identities=tuple(identities))
