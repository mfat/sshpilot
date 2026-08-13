"""Pure, GTK-free reconciliation of app identities and SSH projections.

This module is deliberately not wired into the public connection ID API yet.
It provides the backend prototype boundary: a durable app identity owns
metadata, while a parsed SSH projection is only the current configuration
evidence used to reconcile that identity after external edits.

The matcher never performs I/O, DNS, subprocesses, or network operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .models import ConnectionRecord


class MatchReason(str, Enum):
    """Observable reason for preserving an identity across projections."""

    EXPLICIT_IN_APP_CONTINUITY = "explicit_in_app_continuity"
    EXACT_ALIAS = "exact_alias"
    DESTINATION_USER_IDENTITY = "destination_user_identity"
    DESTINATION_USER = "destination_user"
    DESTINATION_ORDER_FALLBACK = "destination_order_fallback"
    CREATE = "create"
    DELETE = "delete"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ConnectionIdentityProjection:
    """Concrete, materialized SSH connection data used as evidence.

    ``hostname`` and ``port`` are optional because a trustworthy destination
    anchor may not be derivable from a static loader projection.  Only the
    port is normalized: omitted and explicit 22 are equivalent.  Hostname is
    intentionally compared literally.
    """

    alias: str
    hostname: Optional[str]
    port: Optional[int]
    username: str = ""
    identity_files: Tuple[str, ...] = ()
    declaration_order: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not self.alias:
            raise ValueError("projection alias must be a non-empty string")
        if self.hostname is not None and not isinstance(self.hostname, str):
            raise TypeError("projection hostname must be a string or None")
        if not isinstance(self.username, str):
            raise TypeError("projection username must be a string")
        if type(self.declaration_order) is not int:
            raise TypeError("projection declaration order must be an integer")
        if type(self.identity_files) is not tuple:
            raise TypeError("projection identity files must be a tuple")

    @property
    def destination_anchor(self) -> Optional[Tuple[str, int]]:
        """Return a literal static anchor, or ``None`` when it is unsafe."""

        if not self.hostname:
            return None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            return None
        return self.hostname, 22 if self.port == 22 else self.port

    @classmethod
    def from_record(
        cls,
        record: ConnectionRecord,
        *,
        declaration_order: int,
    ) -> "ConnectionIdentityProjection":
        """Build a projection from the actual core loader record.

        Newer records may carry non-serialized literal parser evidence on the
        model.  Older records fall back to their existing parsed values; the
        report documents the resulting environment-dependence.
        """

        raw_identity_files = getattr(record, "raw_identity_files", ())
        if raw_identity_files:
            identity_files = tuple(
                value
                for value in raw_identity_files
                if str(value).strip().lower() != "none"
            )
        else:
            identity_files = tuple(
                str(value)
                for value in (record.data.get("identity_files", ()) or ())
            )
        raw_port = getattr(record, "raw_port", None)
        if raw_port is None:
            port: Optional[int] = int(record.port)
        else:
            try:
                port = int(str(raw_port).strip())
            except (TypeError, ValueError):
                port = None
        raw_username = getattr(record, "raw_username", None)
        username = record.username if raw_username is None else raw_username
        return cls(
            alias=record.id,
            hostname=record.hostname or None,
            port=port,
            username=username,
            identity_files=identity_files,
            declaration_order=declaration_order,
            source=record.source,
        )


@dataclass(frozen=True)
class IdentityRegistryEntry:
    """Durable app identity plus its last-known SSH projection."""

    uuid: str
    projection: ConnectionIdentityProjection
    display_name: str = ""
    tombstone: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.uuid, str) or not self.uuid:
            raise ValueError("identity UUID must be a non-empty string")


@dataclass(frozen=True)
class Match:
    old: IdentityRegistryEntry
    new_projection: ConnectionIdentityProjection
    reason: MatchReason


@dataclass(frozen=True)
class Ambiguous:
    old: Tuple[IdentityRegistryEntry, ...]
    new: Tuple[ConnectionIdentityProjection, ...]
    reason: MatchReason = MatchReason.AMBIGUOUS


@dataclass(frozen=True)
class ReconciliationResult:
    matched: Tuple[Match, ...] = ()
    created: Tuple[IdentityRegistryEntry, ...] = ()
    deleted: Tuple[IdentityRegistryEntry, ...] = ()
    ambiguous: Tuple[Ambiguous, ...] = ()


@dataclass(frozen=True)
class IdentityRegistry:
    """Serializable prototype registry; filesystem ownership stays outside."""

    entries: Tuple[IdentityRegistryEntry, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported identity registry version")
        uuids = [entry.uuid for entry in self.entries]
        if len(uuids) != len(set(uuids)):
            raise ValueError("identity registry UUIDs must be unique")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "version": self.version,
            "identities": [
                {
                    "uuid": entry.uuid,
                    "display_name": entry.display_name,
                    "tombstone": entry.tombstone,
                    "projection": {
                        "alias": entry.projection.alias,
                        "hostname": entry.projection.hostname,
                        "port": entry.projection.port,
                        "username": entry.projection.username,
                        "identity_files": list(entry.projection.identity_files),
                        "declaration_order": entry.projection.declaration_order,
                        "source": entry.projection.source,
                    },
                }
                for entry in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, payload: Optional[Mapping[str, Any]]) -> "IdentityRegistry":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise ValueError("identity registry must be an object")
        if payload.get("version", 1) != 1:
            raise ValueError("unsupported identity registry version")
        raw_entries = payload.get("identities", [])
        if not isinstance(raw_entries, list):
            raise ValueError("identity registry identities must be an array")
        entries = []
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                raise ValueError("identity registry entry must be an object")
            projection = raw.get("projection")
            if not isinstance(projection, Mapping):
                raise ValueError("identity registry projection must be an object")
            identity_files = projection.get("identity_files", [])
            if not isinstance(identity_files, list):
                raise ValueError("identity registry identity files must be an array")
            entries.append(
                IdentityRegistryEntry(
                    uuid=str(raw.get("uuid", "")),
                    display_name=str(raw.get("display_name", "")),
                    tombstone=bool(raw.get("tombstone", False)),
                    projection=ConnectionIdentityProjection(
                        alias=str(projection.get("alias", "")),
                        hostname=projection.get("hostname"),
                        port=projection.get("port"),
                        username=str(projection.get("username", "")),
                        identity_files=tuple(str(item) for item in identity_files),
                        declaration_order=int(projection.get("declaration_order", 0)),
                        source=str(projection.get("source", "")),
                    ),
                )
            )
        return cls(entries=tuple(entries))


def _projection_sort_key(
    item: Tuple[int, ConnectionIdentityProjection],
) -> Tuple[int, str, str, int]:
    index, projection = item
    # Declaration order is the stated fallback.  Alias/source only make a
    # malformed equal-order snapshot deterministic; source is never compared
    # as reconciliation evidence.
    return projection.declaration_order, projection.alias, projection.source, index


def _pair_bucket(
    old: Sequence[Tuple[int, IdentityRegistryEntry]],
    new: Sequence[Tuple[int, ConnectionIdentityProjection]],
    reason: MatchReason,
) -> Iterable[Match]:
    old_sorted = sorted(
        old,
        key=lambda item: (
            item[1].projection.declaration_order,
            item[1].projection.alias,
            item[1].projection.source,
            item[0],
        ),
    )
    new_sorted = sorted(new, key=_projection_sort_key)
    for (_, old_entry), (_, new_projection) in zip(old_sorted, new_sorted):
        yield Match(old_entry, new_projection, reason)


def reconcile_identities(
    old_entries: Sequence[IdentityRegistryEntry],
    new_projections: Sequence[ConnectionIdentityProjection],
    *,
    uuid_factory: Callable[[], str],
) -> ReconciliationResult:
    """Reconcile one old snapshot with one new snapshot deterministically.

    Exact aliases consume first.  Remaining candidates are grouped by the
    literal ``(hostname, normalized_port)`` anchor and consumed in three
    deterministic passes: ordered identity files plus user, user, then
    declaration order.  Tombstones are intentionally excluded.
    """

    active_old = [
        (index, entry)
        for index, entry in enumerate(old_entries)
        if not entry.tombstone
    ]
    old_aliases = [entry.projection.alias for _, entry in active_old]
    new_aliases = [projection.alias for projection in new_projections]
    if len(old_aliases) != len(set(old_aliases)):
        raise ValueError("active identity aliases must be unique")
    if len(new_aliases) != len(set(new_aliases)):
        raise ValueError("new projection aliases must be unique")

    old_by_alias = {entry.projection.alias: (index, entry) for index, entry in active_old}
    used_old = set()
    used_new = set()
    matches = []
    for new_index, projection in enumerate(new_projections):
        old_item = old_by_alias.get(projection.alias)
        if old_item is None:
            continue
        old_index, old_entry = old_item
        used_old.add(old_index)
        used_new.add(new_index)
        matches.append(Match(old_entry, projection, MatchReason.EXACT_ALIAS))

    old_remaining = [item for item in active_old if item[0] not in used_old]
    new_remaining = [
        (index, projection)
        for index, projection in enumerate(new_projections)
        if index not in used_new
    ]
    old_by_anchor: Dict[Tuple[str, int], list] = {}
    new_by_anchor: Dict[Tuple[str, int], list] = {}
    for item in old_remaining:
        anchor = item[1].projection.destination_anchor
        if anchor is not None:
            old_by_anchor.setdefault(anchor, []).append(item)
    for item in new_remaining:
        anchor = item[1].destination_anchor
        if anchor is not None:
            new_by_anchor.setdefault(anchor, []).append(item)

    matched_old = set(used_old)
    matched_new = set(used_new)
    for anchor in sorted(set(old_by_anchor) & set(new_by_anchor), key=repr):
        old_bucket = old_by_anchor[anchor]
        new_bucket = new_by_anchor[anchor]
        remaining_old = old_bucket
        remaining_new = new_bucket
        for old_key_fn, new_key_fn, reason in (
            (
                lambda entry: (
                    entry.projection.username,
                    entry.projection.identity_files,
                ),
                lambda entry: (entry.username, entry.identity_files),
                MatchReason.DESTINATION_USER_IDENTITY,
            ),
            (
                lambda entry: entry.projection.username,
                lambda entry: entry.username,
                MatchReason.DESTINATION_USER,
            ),
        ):
            old_partitions: Dict[Any, list] = {}
            new_partitions: Dict[Any, list] = {}
            for item in remaining_old:
                old_partitions.setdefault(old_key_fn(item[1]), []).append(item)
            for item in remaining_new:
                new_partitions.setdefault(new_key_fn(item[1]), []).append(item)
            for key in sorted(set(old_partitions) & set(new_partitions), key=repr):
                paired = list(
                    _pair_bucket(old_partitions[key], new_partitions[key], reason)
                )
                matches.extend(paired)
                paired_old = {id(match.old) for match in paired}
                paired_new = {id(match.new_projection) for match in paired}
                remaining_old = [
                    item for item in remaining_old if id(item[1]) not in paired_old
                ]
                remaining_new = [
                    item for item in remaining_new if id(item[1]) not in paired_new
                ]
                for old_index, old_entry in old_partitions[key]:
                    if id(old_entry) in paired_old:
                        matched_old.add(old_index)
                for new_index, new_projection in new_partitions[key]:
                    if id(new_projection) in paired_new:
                        matched_new.add(new_index)
        paired = list(
            _pair_bucket(
                remaining_old,
                remaining_new,
                MatchReason.DESTINATION_ORDER_FALLBACK,
            )
        )
        matches.extend(paired)
        for old_index, old_entry in remaining_old:
            if any(match.old is old_entry for match in paired):
                matched_old.add(old_index)
        for new_index, new_projection in remaining_new:
            if any(match.new_projection is new_projection for match in paired):
                matched_new.add(new_index)

    deleted = tuple(
        entry for index, entry in active_old if index not in matched_old
    )
    created = tuple(
        IdentityRegistryEntry(
            uuid=str(uuid_factory()),
            projection=projection,
            display_name=projection.alias,
        )
        for index, projection in enumerate(new_projections)
        if index not in matched_new
    )
    matches.sort(key=lambda match: (match.new_projection.declaration_order, match.new_projection.alias))
    return ReconciliationResult(
        matched=tuple(matches),
        created=created,
        deleted=deleted,
        ambiguous=(),
    )


def apply_reconciliation(result: ReconciliationResult) -> IdentityRegistry:
    """Build the next active registry, preserving metadata on matches.

    Deleted entries are omitted from the active registry.  Callers may retain
    them separately as tombstones for diagnostics, but the matcher never uses
    those tombstones as future evidence.
    """

    entries = [
        IdentityRegistryEntry(
            uuid=match.old.uuid,
            projection=match.new_projection,
            display_name=match.old.display_name,
        )
        for match in result.matched
    ]
    entries.extend(result.created)
    return IdentityRegistry(entries=tuple(entries))


def registry_from_records(
    records: Sequence[ConnectionRecord],
    *,
    uuid_factory: Callable[[], str],
    display_names: Optional[Mapping[str, str]] = None,
) -> IdentityRegistry:
    """Create a registry from actual loader records for restart prototypes."""

    names = display_names or {}
    entries = tuple(
        IdentityRegistryEntry(
            uuid=str(uuid_factory()),
            projection=ConnectionIdentityProjection.from_record(
                record, declaration_order=index
            ),
            display_name=names.get(record.id, record.id),
        )
        for index, record in enumerate(records)
        if record.protocol == "ssh"
    )
    return IdentityRegistry(entries=entries)
