"""Lossless known-hosts document model and removal (GTK-free).

A :class:`KnownHostsDocument` captures the exact input bytes together with a
stable revision (the SHA-256 digest of those bytes) and the displayed lines as
entry records keyed by a deterministic entry id. Serialization always returns
original bytes, so comments, blank lines, malformed lines, LF/CRLF endings,
ordering, and final-newline presence survive byte-for-byte until a removal is
applied.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Tuple

from .service import KnownHostEntry


@dataclass(frozen=True)
class KnownHostsDocumentEntry:
    """One displayed physical line of the known-hosts document."""

    entry_id: str
    line_index: int
    parsed: KnownHostEntry


@dataclass(frozen=True)
class KnownHostsDocument:
    """The exact bytes of the document plus its displayed entries."""

    original: bytes = field(repr=False)
    revision: str
    entries: Tuple[KnownHostsDocumentEntry, ...]


def _physical_lines(content: bytes) -> list[bytes]:
    """Split *content* into physical lines, each retaining its terminator.

    A trailing ``\\n`` does not produce a phantom empty final line; a file with
    no final newline keeps its last partial line.
    """

    lines: list[bytes] = []
    start = 0
    size = len(content)
    index = 0
    while index < size:
        if content[index] == 0x0A:
            lines.append(content[start : index + 1])
            start = index + 1
        index += 1
    if start < size:
        lines.append(content[start:])
    return lines


def _display_bytes(line: bytes) -> bytes:
    """Return *line* without its single trailing LF/CRLF terminator."""

    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith(b"\n"):
        return line[:-1]
    return line


def parse_known_hosts_document(content: bytes) -> KnownHostsDocument:
    """Parse exact known-hosts bytes into a lossless document."""

    revision = hashlib.sha256(content).hexdigest()
    entries: list[KnownHostsDocumentEntry] = []
    for line_index, raw in enumerate(_physical_lines(content)):
        display_bytes = _display_bytes(raw)
        if not display_bytes.strip():
            continue
        if display_bytes.lstrip().startswith(b"#"):
            continue
        display = display_bytes.decode("utf-8", errors="replace")
        entries.append(
            KnownHostsDocumentEntry(
                entry_id=f"{revision}:{line_index}",
                line_index=line_index,
                parsed=KnownHostEntry.parse(display),
            )
        )
    return KnownHostsDocument(
        original=content,
        revision=revision,
        entries=tuple(entries),
    )


def remove_known_hosts_document_entries(
    document: KnownHostsDocument,
    entry_ids: Tuple[str, ...],
) -> bytes:
    """Remove the physical lines selected by *entry_ids* from *document*.

    Unknown or duplicate requested ids raise :class:`ValueError`. Everything
    else in the document is preserved byte-for-byte.
    """

    by_id = {entry.entry_id: entry for entry in document.entries}
    unknown = [entry_id for entry_id in entry_ids if entry_id not in by_id]
    if unknown:
        raise ValueError(f"unknown known-hosts entry id: {unknown[0]!r}")
    if len(set(entry_ids)) != len(entry_ids):
        raise ValueError("known-hosts entry ids must not repeat")
    remove_indices = {by_id[entry_id].line_index for entry_id in entry_ids}
    kept = [
        raw
        for line_index, raw in enumerate(_physical_lines(document.original))
        if line_index not in remove_indices
    ]
    return b"".join(kept)
