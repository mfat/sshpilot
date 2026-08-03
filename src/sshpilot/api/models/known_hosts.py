"""Frontend-neutral known-hosts protocol models.

These models describe the lossless known-hosts document through a stable
revision token (the SHA-256 digest of the exact file bytes) and per-line entry
identifiers. The GTK view never touches the file; it renders summaries and
sends batched removal requests against a revision it loaded.
"""

from dataclasses import dataclass, field
from typing import NewType, Tuple

from .common import require_identifier

KnownHostEntryId = NewType("KnownHostEntryId", str)

_TEXT_FIELDS = ("hostname", "key_type", "display_line")


@dataclass(frozen=True)
class KnownHostEntrySummary:
    """One displayed known-hosts line (never the raw file handle)."""

    entry_id: KnownHostEntryId
    hostname: str
    key_type: str
    display_line: str = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.entry_id, "known-hosts entry id")
        for field_name in _TEXT_FIELDS:
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a string")
            if "\x00" in value:
                raise ValueError(f"{field_name} must not contain NUL")
        if not self.display_line.strip():
            raise ValueError("display line must be a non-empty string")


@dataclass(frozen=True)
class KnownHostsSnapshot:
    """The full known-hosts document projected for display."""

    revision: str
    entries: Tuple[KnownHostEntrySummary, ...] = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.revision, "known-hosts revision")
        if type(self.entries) is not tuple:
            raise TypeError("known-hosts entries must be a tuple")
        for entry in self.entries:
            if type(entry) is not KnownHostEntrySummary:
                raise TypeError("known-hosts entries must be KnownHostEntrySummary")


@dataclass(frozen=True)
class RemoveKnownHostEntriesRequest:
    """Batch removal against one loaded revision of the document."""

    revision: str
    entry_ids: Tuple[KnownHostEntryId, ...]

    def __post_init__(self) -> None:
        require_identifier(self.revision, "known-hosts revision")
        if type(self.entry_ids) is not tuple:
            raise TypeError("known-hosts entry ids must be a tuple")
        if not self.entry_ids:
            raise ValueError("known-hosts removal requires at least one entry id")
        for entry_id in self.entry_ids:
            require_identifier(entry_id, "known-hosts entry id")
        if len(set(self.entry_ids)) != len(self.entry_ids):
            raise ValueError("known-hosts removal entry ids must not repeat")


@dataclass(frozen=True)
class KnownHostsMutationResult:
    """Result of a committed removal, re-read from the authoritative file."""

    revision: str
    removed_count: int
    entries: Tuple[KnownHostEntrySummary, ...] = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.revision, "known-hosts revision")
        if type(self.removed_count) is not int or self.removed_count < 0:
            raise ValueError("removed count must be a non-negative integer")
        if type(self.entries) is not tuple:
            raise TypeError("known-hosts entries must be a tuple")
        for entry in self.entries:
            if type(entry) is not KnownHostEntrySummary:
                raise TypeError("known-hosts entries must be KnownHostEntrySummary")
