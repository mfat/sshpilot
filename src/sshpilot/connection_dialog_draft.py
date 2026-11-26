"""The connection editor's unsaved lines, as OpenSSH would read them.

The dialog edits one ``Host`` block. What the host *uses* for a setting is
OpenSSH's to say: an earlier block, a ``Match`` block or the system-wide
file may decide it. For the saved file ``ssh -G`` answers; for an edit not
saved yet nothing does, unless the edit is put into a copy of the
configuration and OpenSSH is asked about that copy. This module builds the
copy (text only) and reads the answer; the asking is the daemon's draft
evaluator (``connections.evaluate_ssh_config_draft``), the same one
Advanced Configuration uses, so the draft is read as a launch reads the
saved file.

Two questions are asked about a host:

* **effective**: the configuration with the block's pending lines. What
  OpenSSH would use once the dialog is saved.
* **inherited**: the same with the block's authentication lines left out.
  What the host gets when it sets none itself, which is what a checkbox
  needs to know before it drops or writes a line.

Only a block of the root file can be put into a draft: the evaluator takes
the root's text and reads included files from disk. For a host kept in an
included file :func:`draft_for_host` returns ``None`` and the caller falls
back to what it knows about the saved file.

No GTK here: the rules are tested headless.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# The authentication keywords, as the authentication section knows them.
from .connection_dialog_auth import AUTH_POLICY_KEYWORDS, canonical_keyword
from .ssh_config_blocks import HOST, scan_blocks
from .ssh_config_document import split_config_lines, split_host_tokens

#: The identity lines the dialog edits besides the authentication ones.
IDENTITY_KEYWORDS = frozenset({"identityfile", "identitiesonly"})

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


def has_own_block(text: str, host: str) -> bool:
    return bool(host) and bool(_own_blocks(text, host))


def draft_for_host(
    root_text: str,
    host: str,
    replaced: Iterable[str],
    lines: Sequence[str],
    *,
    new: bool = False,
) -> Optional[str]:
    """*root_text* with *host*'s own lines for *replaced* swapped for *lines*.

    Every line of the host's own ``Host`` blocks whose keyword is in
    *replaced* (lower case; either spelling of a renamed keyword) is taken
    out, and *lines* are written at the top of its first block. ``new``: the
    host has no block yet; one is appended after everything else, where a
    save puts it. ``None`` when the host is not new and the root holds no
    block of its own (it is kept in an included file).
    """
    host = (host or "").strip()
    if not host or any(ch.isspace() for ch in host):
        return None
    replaced_settings = {canonical_keyword(k) for k in replaced}
    text = root_text or ""
    new_lines = [f"    {line.strip()}" for line in lines if line and line.strip()]
    own = _own_blocks(text, host)
    if not own:
        if not new:
            return None
        tail = "" if not text or text.endswith("\n") else "\n"
        return text + tail + "\n".join([f"Host {host}", *new_lines]) + "\n"
    dropped = {
        item.line
        for block in own
        for item in block.lines
        if canonical_keyword(item.keyword) in replaced_settings
    }
    header = own[0].line
    out: List[str] = []
    for number, raw in enumerate(split_config_lines(text, keepends=True), start=1):
        if number in dropped:
            continue
        out.append(raw)
        if number == header:
            if not raw.endswith("\n"):
                out[-1] = raw + "\n"
            out.extend(line + "\n" for line in new_lines)
    return "".join(out)


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


@dataclass(frozen=True)
class HostDrafts:
    """The two drafts to evaluate for a host; see the module docstring.

    ``inherited`` equals ``effective`` when the block has no authentication
    line: one question then answers both.
    """

    effective: str
    inherited: str


def build_drafts(
    root_text: str,
    host: str,
    auth_lines: Sequence[str],
    identity_lines: Optional[Sequence[str]] = None,
    *,
    new: bool = False,
) -> Optional[HostDrafts]:
    """Drafts for *host* with the dialog's pending lines; ``None`` when the
    host's block is not in the root file.

    ``identity_lines``: the block's ``IdentityFile``/``IdentitiesOnly`` lines
    as the dialog would write them, or ``None`` when it did not change them
    (the saved lines then stay as written).
    """
    replaced = set(_AUTH_KEYWORDS)
    identity: List[str] = []
    if identity_lines is not None:
        replaced |= set(IDENTITY_KEYWORDS)
        identity = [line for line in identity_lines if line and line.strip()]
    auth = [line for line in auth_lines if line and line.strip()]
    inherited = draft_for_host(root_text, host, replaced, identity, new=new)
    if inherited is None:
        return None
    if not auth:
        return HostDrafts(effective=inherited, inherited=inherited)
    effective = draft_for_host(root_text, host, replaced, [*identity, *auth], new=new)
    if effective is None:
        return None
    return HostDrafts(effective=effective, inherited=inherited)
