"""Pure, GTK-free reconciliation of app identities and SSH projections.

This module is deliberately not wired into the public connection ID API yet.
It provides the backend prototype boundary: a durable app identity owns
metadata, while a parsed SSH projection is only the current configuration
evidence used to reconcile that identity after external edits.

The matcher never performs I/O, DNS, subprocesses, or network operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .models import ConnectionRecord


class MatchReason(str, Enum):
    """Observable reason for preserving an identity across projections."""

    EXPLICIT_IN_APP_CONTINUITY = "explicit_in_app_continuity"
    EXACT_ALIAS = "exact_alias"
    DESTINATION_USER_IDENTITY = "destination_user_identity"
    DESTINATION_USER = "destination_user"
    DESTINATION_UNIQUE_REMAINDER = "destination_unique_remainder"
    CREATE = "create"
    DELETE = "delete"
    AMBIGUOUS = "ambiguous"


class DestinationEvidenceStatus(str, Enum):
    TRUSTWORTHY = "trustworthy"
    UNAVAILABLE = "unavailable"


class DestinationEvidenceReason(str, Enum):
    EXPLICIT_STATIC = "explicit_static"
    MISSING_HOSTNAME = "missing_hostname"
    HOST_DEPENDENT_HOSTNAME = "host_dependent_hostname"
    INHERITED_CONFIGURATION = "inherited_configuration"
    INCLUDE_SEMANTICS = "include_semantics"
    DYNAMIC_MATCH = "dynamic_match"
    REPEATED_HOST = "repeated_host"
    GLOBAL_CONFIGURATION = "global_configuration"
    INVALID_PORT = "invalid_port"
    NOT_PROVIDED = "not_provided"


class IdentityFileEvidenceStatus(str, Enum):
    SAFE_STATIC_LITERAL = "safe_static_literal"
    DYNAMIC = "dynamic"
    UNAVAILABLE = "unavailable"


class IdentityFileEvidenceMode(str, Enum):
    """Semantic meaning of the IdentityFile directives in a projection."""

    UNSPECIFIED = "unspecified"
    EXPLICIT_NONE = "explicit_none"
    EXPLICIT_FILES = "explicit_files"
    DYNAMIC = "dynamic"


def _is_static_identity_expression(value: str) -> bool:
    """Return whether a raw IdentityFile expression is deterministic config intent.

    ``~`` is intentionally accepted: this prototype compares configuration
    intent, not the physical key path resolved under a possibly different
    home directory. Environment variables and every percent token remain
    dynamic evidence.
    """

    if "$" in value:
        return False
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "%":
            index += 2
            continue
        return False
    return True


@dataclass(frozen=True)
class DestinationAnchor:
    hostname: str
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.hostname, str) or not self.hostname:
            raise ValueError("destination hostname must be a non-empty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("destination port must be between 1 and 65535")

    def as_tuple(self) -> Tuple[str, int]:
        return self.hostname, 22 if self.port == 22 else self.port


@dataclass(frozen=True)
class StaticDestinationEvidence:
    status: DestinationEvidenceStatus
    reason: DestinationEvidenceReason
    anchor: Optional[DestinationAnchor] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DestinationEvidenceStatus):
            raise TypeError("destination evidence status must be an enum")
        if not isinstance(self.reason, DestinationEvidenceReason):
            raise TypeError("destination evidence reason must be an enum")
        if self.status is DestinationEvidenceStatus.TRUSTWORTHY:
            if self.anchor is None:
                raise ValueError("trustworthy destination evidence needs an anchor")
            if self.reason is not DestinationEvidenceReason.EXPLICIT_STATIC:
                raise ValueError("trustworthy evidence must be explicit static evidence")
        else:
            if self.reason is DestinationEvidenceReason.EXPLICIT_STATIC:
                raise ValueError("unavailable evidence cannot claim explicit static proof")
            if self.anchor is not None:
                raise ValueError("unavailable destination evidence cannot carry an anchor")

    @classmethod
    def trustworthy(cls, hostname: str, port: int) -> "StaticDestinationEvidence":
        if not hostname or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("trustworthy destination evidence must be valid")
        return cls(
            status=DestinationEvidenceStatus.TRUSTWORTHY,
            reason=DestinationEvidenceReason.EXPLICIT_STATIC,
            anchor=DestinationAnchor(hostname, port),
        )

    @classmethod
    def unavailable(
        cls,
        reason: DestinationEvidenceReason = DestinationEvidenceReason.NOT_PROVIDED,
    ) -> "StaticDestinationEvidence":
        return cls(
            status=DestinationEvidenceStatus.UNAVAILABLE,
            reason=reason,
            anchor=None,
        )


@dataclass(frozen=True)
class IdentityFileEvidence:
    mode: IdentityFileEvidenceMode
    values: Tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.mode, IdentityFileEvidenceMode):
            raise TypeError("IdentityFile evidence mode must be an enum")
        if type(self.values) is not tuple:
            raise TypeError("IdentityFile evidence values must be a tuple")
        if any(not isinstance(value, str) or not value for value in self.values):
            raise ValueError("IdentityFile evidence values must be non-empty strings")
        if self.mode in {
            IdentityFileEvidenceMode.UNSPECIFIED,
            IdentityFileEvidenceMode.EXPLICIT_NONE,
            IdentityFileEvidenceMode.DYNAMIC,
        } and self.values:
            raise ValueError(f"{self.mode.value} IdentityFile evidence cannot carry values")
        if self.mode is IdentityFileEvidenceMode.EXPLICIT_FILES:
            if not self.values:
                raise ValueError("explicit IdentityFile evidence needs values")
            if any(value.strip().lower() == "none" for value in self.values):
                raise ValueError("explicit file evidence cannot contain IdentityFile none")
            if any(not _is_static_identity_expression(value) for value in self.values):
                raise ValueError("dynamic IdentityFile expressions cannot be static evidence")

    @property
    def status(self) -> IdentityFileEvidenceStatus:
        """Compatibility view of the semantic mode used by the earlier prototype."""

        if self.mode in {
            IdentityFileEvidenceMode.EXPLICIT_NONE,
            IdentityFileEvidenceMode.EXPLICIT_FILES,
        }:
            return IdentityFileEvidenceStatus.SAFE_STATIC_LITERAL
        if self.mode is IdentityFileEvidenceMode.DYNAMIC:
            return IdentityFileEvidenceStatus.DYNAMIC
        return IdentityFileEvidenceStatus.UNAVAILABLE

    @classmethod
    def unspecified(cls, reason: str = "no IdentityFile directive") -> "IdentityFileEvidence":
        return cls(IdentityFileEvidenceMode.UNSPECIFIED, (), reason)

    @classmethod
    def explicit_none(cls, reason: str = "IdentityFile none") -> "IdentityFileEvidence":
        return cls(IdentityFileEvidenceMode.EXPLICIT_NONE, (), reason)

    @classmethod
    def safe(cls, values: Iterable[str]) -> "IdentityFileEvidence":
        literal_values = tuple(values)
        if not literal_values:
            return cls.unspecified()
        return cls(
            IdentityFileEvidenceMode.EXPLICIT_FILES,
            literal_values,
            "static literal",
        )

    @classmethod
    def dynamic(cls, reason: str) -> "IdentityFileEvidence":
        return cls(IdentityFileEvidenceMode.DYNAMIC, (), reason)

    @classmethod
    def unavailable(cls, reason: str = "not provided") -> "IdentityFileEvidence":
        return cls.unspecified(reason)


def _enum_or_default(enum_type, value: str, default):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _enum_from_payload(enum_type, value: Any, field_name: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc


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
    destination_evidence: StaticDestinationEvidence = field(
        default_factory=StaticDestinationEvidence.unavailable
    )
    username_literal: Optional[str] = None
    username_is_explicit: bool = False
    identity_file_evidence: IdentityFileEvidence = field(
        default_factory=IdentityFileEvidence.unavailable
    )

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
        if not isinstance(self.destination_evidence, StaticDestinationEvidence):
            raise TypeError("destination evidence must be StaticDestinationEvidence")
        if self.destination_evidence.status is DestinationEvidenceStatus.TRUSTWORTHY:
            if not isinstance(self.hostname, str) or not self.hostname:
                raise ValueError(
                    "trustworthy destination evidence requires a non-empty literal hostname"
                )
            if type(self.port) is not int or not 1 <= self.port <= 65535:
                raise ValueError(
                    "trustworthy destination evidence requires a literal port between 1 and 65535"
                )
            literal_anchor = DestinationAnchor(self.hostname, self.port).as_tuple()
            if literal_anchor != self.destination_evidence.anchor.as_tuple():
                raise ValueError(
                    "projection literal destination must match trustworthy "
                    "destination evidence anchor"
                )
        if not isinstance(self.identity_file_evidence, IdentityFileEvidence):
            raise TypeError("identity file evidence must be IdentityFileEvidence")
        if self.username_is_explicit and not self.username_literal:
            raise ValueError("explicit username evidence requires a literal")

    @property
    def destination_anchor(self) -> Optional[Tuple[str, int]]:
        """Return a literal static anchor, or ``None`` when it is unsafe."""

        if self.destination_evidence.status is not DestinationEvidenceStatus.TRUSTWORTHY:
            return None
        if self.destination_evidence.anchor is None:
            return None
        return self.destination_evidence.anchor.as_tuple()

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
        destination_status = _enum_or_default(
            DestinationEvidenceStatus,
            getattr(record, "static_destination_status", "unavailable"),
            DestinationEvidenceStatus.UNAVAILABLE,
        )
        destination_reason = _enum_or_default(
            DestinationEvidenceReason,
            getattr(record, "static_destination_reason", "not_provided"),
            DestinationEvidenceReason.NOT_PROVIDED,
        )
        if destination_status is DestinationEvidenceStatus.TRUSTWORTHY:
            if not record.hostname or port is None:
                destination_evidence = StaticDestinationEvidence.unavailable(
                    DestinationEvidenceReason.NOT_PROVIDED
                )
            else:
                destination_evidence = StaticDestinationEvidence.trustworthy(
                    record.hostname, port
                )
        else:
            destination_evidence = StaticDestinationEvidence.unavailable(
                destination_reason
            )
        identity_status = _enum_or_default(
            IdentityFileEvidenceStatus,
            getattr(record, "identity_file_evidence_status", "unavailable"),
            IdentityFileEvidenceStatus.UNAVAILABLE,
        )
        identity_mode = _enum_or_default(
            IdentityFileEvidenceMode,
            getattr(record, "identity_file_evidence_mode", "unspecified"),
            IdentityFileEvidenceMode.UNSPECIFIED,
        )
        if (
            identity_mode is IdentityFileEvidenceMode.UNSPECIFIED
            and identity_status is IdentityFileEvidenceStatus.SAFE_STATIC_LITERAL
            and identity_files
        ):
            # Records produced by the previous prototype had no mode field.
            # Non-empty safe values are unambiguously explicit files; an old
            # empty safe tuple remains conservatively unspecified.
            identity_mode = IdentityFileEvidenceMode.EXPLICIT_FILES
        elif (
            identity_mode is IdentityFileEvidenceMode.UNSPECIFIED
            and identity_status is IdentityFileEvidenceStatus.DYNAMIC
        ):
            identity_mode = IdentityFileEvidenceMode.DYNAMIC
        if identity_mode is IdentityFileEvidenceMode.EXPLICIT_NONE:
            identity_evidence = IdentityFileEvidence.explicit_none(
                getattr(record, "identity_file_evidence_reason", "explicit_none")
            )
        elif identity_mode is IdentityFileEvidenceMode.EXPLICIT_FILES:
            identity_evidence = IdentityFileEvidence(
                IdentityFileEvidenceMode.EXPLICIT_FILES,
                tuple(identity_files),
                getattr(record, "identity_file_evidence_reason", "static literal"),
            )
        elif identity_mode is IdentityFileEvidenceMode.DYNAMIC:
            identity_evidence = IdentityFileEvidence.dynamic(
                getattr(record, "identity_file_evidence_reason", "dynamic")
            )
        else:
            identity_evidence = IdentityFileEvidence.unspecified(
                getattr(record, "identity_file_evidence_reason", "unavailable")
            )
        return cls(
            alias=record.id,
            hostname=record.hostname or None,
            port=port,
            username=username,
            identity_files=identity_files,
            declaration_order=declaration_order,
            source=record.source,
            destination_evidence=destination_evidence,
            username_literal=getattr(record, "username_literal", None),
            username_is_explicit=bool(
                getattr(record, "username_is_explicit", False)
            ),
            identity_file_evidence=identity_evidence,
        )


@dataclass(frozen=True)
class IdentityRegistryEntry:
    """Durable app identity plus its last-known SSH projection."""

    uuid: str
    projection: ConnectionIdentityProjection
    display_name: str = ""
    tombstone: bool = False
    retired_generation: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.uuid, str) or not self.uuid:
            raise ValueError("identity UUID must be a non-empty string")
        if self.retired_generation is not None and (
            type(self.retired_generation) is not int or self.retired_generation < 0
        ):
            raise ValueError("retired generation must be a non-negative integer")


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
                    **(
                        {"retired_generation": entry.retired_generation}
                        if entry.retired_generation is not None
                        else {}
                    ),
                    "projection": {
                        "alias": entry.projection.alias,
                        "hostname": entry.projection.hostname,
                        "port": entry.projection.port,
                        "username": entry.projection.username,
                        "username_literal": entry.projection.username_literal,
                        "username_is_explicit": entry.projection.username_is_explicit,
                        "identity_files": list(entry.projection.identity_files),
                        "identity_file_evidence_mode": entry.projection.identity_file_evidence.mode.value,
                        "identity_file_evidence_status": entry.projection.identity_file_evidence.status.value,
                        "identity_file_evidence_values": list(entry.projection.identity_file_evidence.values),
                        "identity_file_evidence_reason": entry.projection.identity_file_evidence.reason,
                        "destination_evidence_status": entry.projection.destination_evidence.status.value,
                        "destination_evidence_reason": entry.projection.destination_evidence.reason.value,
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
            identity_evidence_values = projection.get(
                "identity_file_evidence_values", identity_files
            )
            if not isinstance(identity_evidence_values, list):
                raise ValueError("identity registry identity evidence must be an array")
            evidence_reason = str(
                projection.get("identity_file_evidence_reason", "unavailable")
            )
            if "identity_file_evidence_mode" in projection:
                identity_mode = _enum_from_payload(
                    IdentityFileEvidenceMode,
                    projection["identity_file_evidence_mode"],
                    "identity_file_evidence_mode",
                )
            else:
                # Commit 81e5ae5 serialized only status plus values. An empty
                # safe tuple was ambiguous, so it is deliberately restored as
                # the weaker UNSPECIFIED mode rather than EXPLICIT_NONE.
                legacy_status = _enum_from_payload(
                    IdentityFileEvidenceStatus,
                    projection.get("identity_file_evidence_status", "unavailable"),
                    "identity_file_evidence_status",
                )
                if (
                    legacy_status is IdentityFileEvidenceStatus.SAFE_STATIC_LITERAL
                    and identity_evidence_values
                ):
                    identity_mode = IdentityFileEvidenceMode.EXPLICIT_FILES
                elif legacy_status is IdentityFileEvidenceStatus.DYNAMIC:
                    identity_mode = IdentityFileEvidenceMode.DYNAMIC
                else:
                    identity_mode = IdentityFileEvidenceMode.UNSPECIFIED
            if (
                identity_mode is not IdentityFileEvidenceMode.EXPLICIT_FILES
                and identity_evidence_values
            ):
                raise ValueError(
                    f"{identity_mode.value} IdentityFile evidence cannot carry values"
                )
            if identity_mode is IdentityFileEvidenceMode.EXPLICIT_NONE:
                identity_evidence = IdentityFileEvidence.explicit_none(evidence_reason)
            elif identity_mode is IdentityFileEvidenceMode.EXPLICIT_FILES:
                identity_evidence = IdentityFileEvidence(
                    IdentityFileEvidenceMode.EXPLICIT_FILES,
                    tuple(identity_evidence_values),
                    evidence_reason,
                )
            elif identity_mode is IdentityFileEvidenceMode.DYNAMIC:
                identity_evidence = IdentityFileEvidence.dynamic(evidence_reason)
            else:
                identity_evidence = IdentityFileEvidence.unspecified(evidence_reason)
            destination_status = _enum_from_payload(
                DestinationEvidenceStatus,
                projection.get("destination_evidence_status", "unavailable"),
                "destination_evidence_status",
            )
            destination_reason = _enum_from_payload(
                DestinationEvidenceReason,
                projection.get("destination_evidence_reason", "not_provided"),
                "destination_evidence_reason",
            )
            destination_anchor = None
            if destination_status is DestinationEvidenceStatus.TRUSTWORTHY:
                hostname = projection.get("hostname")
                port = projection.get("port")
                if not isinstance(hostname, str) or type(port) is not int:
                    raise ValueError("trustworthy destination evidence is incomplete")
                destination_anchor = DestinationAnchor(hostname, port)
            entries.append(
                IdentityRegistryEntry(
                    uuid=str(raw.get("uuid", "")),
                    display_name=str(raw.get("display_name", "")),
                    tombstone=bool(raw.get("tombstone", False)),
                    retired_generation=raw.get("retired_generation"),
                    projection=ConnectionIdentityProjection(
                        alias=str(projection.get("alias", "")),
                        hostname=projection.get("hostname"),
                        port=projection.get("port"),
                        username=str(projection.get("username", "")),
                        username_literal=(
                            str(projection["username_literal"])
                            if projection.get("username_literal") is not None
                            else None
                        ),
                        username_is_explicit=bool(
                            projection.get("username_is_explicit", False)
                        ),
                        identity_files=tuple(str(item) for item in identity_files),
                        identity_file_evidence=identity_evidence,
                        destination_evidence=StaticDestinationEvidence(
                            status=destination_status,
                            reason=destination_reason,
                            anchor=destination_anchor,
                        ),
                        declaration_order=int(projection.get("declaration_order", 0)),
                        source=str(projection.get("source", "")),
                    ),
                )
            )
        return cls(entries=tuple(entries))


def _old_entry_sort_key(entry: IdentityRegistryEntry) -> Tuple[int, str, str, str]:
    return (
        entry.projection.declaration_order,
        entry.projection.alias,
        entry.projection.source,
        entry.uuid,
    )


def _new_projection_sort_key(
    projection: ConnectionIdentityProjection,
) -> Tuple[int, str, str]:
    return (
        projection.declaration_order,
        projection.alias,
        projection.source,
    )


def _ambiguous(
    old: Sequence[Tuple[int, IdentityRegistryEntry]],
    new: Sequence[Tuple[int, ConnectionIdentityProjection]],
) -> Ambiguous:
    """Order ambiguity members for diagnostics without mapping them."""

    return Ambiguous(
        old=tuple(sorted((entry for _, entry in old), key=_old_entry_sort_key)),
        new=tuple(
            sorted(
                (projection for _, projection in new),
                key=_new_projection_sort_key,
            )
        ),
    )


def _partition(items: Sequence[Any], key_fn: Callable[[Any], Any]) -> Dict[Any, list]:
    partitions: Dict[Any, list] = {}
    for item in items:
        partitions.setdefault(key_fn(item), []).append(item)
    return partitions


def reconcile_identities(
    old_entries: Sequence[IdentityRegistryEntry],
    new_projections: Sequence[ConnectionIdentityProjection],
    *,
    uuid_factory: Callable[[], str],
) -> ReconciliationResult:
    """Reconcile one old snapshot with one new snapshot deterministically.

    Exact aliases among active identities consume first. Tombstones never
    participate: a tombstone means the user deleted that connection from this
    root, and re-adding the alias is a new connection. Remaining candidates
    are grouped by the literal ``(hostname, normalized_port)`` anchor. Within
    each destination, only a unique evidence partition can establish a
    member-to-member match. Multiple candidates in an otherwise matching
    partition are reserved as an explicit ambiguity; declaration order is
    never identity evidence.

    This function reconciles ONE SSH configuration root against its OWN
    identity state. It has no notion of operation mode, and must not grow
    one: each root owns a separate sidecar, so the other root's identities
    are never in ``old_entries`` and cannot be matched, resurrected, or
    captured here.

    It used to need that notion. While both roots shared one state file, a
    mode switch reconciled a root against the other root's identities, so the
    caller had to disable the destination phase (or an entry merely pointing
    at the same ``(hostname, port, user)`` in the other document captured the
    identity, display name, folder and tags included) and enable tombstone
    resurrection (because leaving a root tombstoned everything in it, and
    returning had to raise it all again). Both knobs are gone with the file
    they existed for.
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
    reserved_old = set()
    reserved_new = set()
    ambiguous = []
    shared_anchors = sorted(set(old_by_anchor) & set(new_by_anchor), key=repr)
    for anchor in shared_anchors:
        old_bucket = old_by_anchor[anchor]
        new_bucket = new_by_anchor[anchor]
        remaining_old = list(old_bucket)
        remaining_new = list(new_bucket)
        for old_key_fn, new_key_fn, reason in (
            (
                lambda entry: (
                    entry.projection.username_literal,
                    entry.projection.identity_file_evidence.mode,
                    entry.projection.identity_file_evidence.values,
                ),
                lambda entry: (
                    entry.username_literal,
                    entry.identity_file_evidence.mode,
                    entry.identity_file_evidence.values,
                ),
                MatchReason.DESTINATION_USER_IDENTITY,
            ),
            (
                lambda entry: entry.projection.username_literal,
                lambda entry: entry.username_literal,
                MatchReason.DESTINATION_USER,
            ),
        ):
            old_partitions = _partition(
                remaining_old, lambda item: old_key_fn(item[1])
            )
            new_partitions = _partition(
                remaining_new, lambda item: new_key_fn(item[1])
            )
            if reason is MatchReason.DESTINATION_USER_IDENTITY:
                comparable_modes = {
                    IdentityFileEvidenceMode.EXPLICIT_NONE,
                    IdentityFileEvidenceMode.EXPLICIT_FILES,
                }
                old_partitions = {
                    key: items
                    for key, items in old_partitions.items()
                    if all(
                        item[1].projection.username_is_explicit
                        and item[1].projection.identity_file_evidence.mode
                        in comparable_modes
                        for item in items
                    )
                }
                new_partitions = {
                    key: items
                    for key, items in new_partitions.items()
                    if all(
                        item[1].username_is_explicit
                        and item[1].identity_file_evidence.mode in comparable_modes
                        for item in items
                    )
                }
            elif reason is MatchReason.DESTINATION_USER:
                old_partitions = {
                    key: items
                    for key, items in old_partitions.items()
                    if all(item[1].projection.username_is_explicit for item in items)
                }
                new_partitions = {
                    key: items
                    for key, items in new_partitions.items()
                    if all(item[1].username_is_explicit for item in items)
                }
            for key in sorted(set(old_partitions) & set(new_partitions), key=repr):
                old_candidates = old_partitions[key]
                new_candidates = new_partitions[key]
                if len(old_candidates) == 1 and len(new_candidates) == 1:
                    old_index, old_entry = old_candidates[0]
                    new_index, new_projection = new_candidates[0]
                    matches.append(Match(old_entry, new_projection, reason))
                    matched_old.add(old_index)
                    matched_new.add(new_index)
                else:
                    ambiguous.append(_ambiguous(old_candidates, new_candidates))
                    reserved_old.update(item[0] for item in old_candidates)
                    reserved_new.update(item[0] for item in new_candidates)
                old_ids = {item[0] for item in old_candidates}
                new_ids = {item[0] for item in new_candidates}
                remaining_old = [
                    item for item in remaining_old if item[0] not in old_ids
                ]
                remaining_new = [
                    item for item in remaining_new if item[0] not in new_ids
                ]
        if len(remaining_old) == 1 and len(remaining_new) == 1:
            old_index, old_entry = remaining_old[0]
            new_index, new_projection = remaining_new[0]
            matches.append(
                Match(
                    old_entry,
                    new_projection,
                    MatchReason.DESTINATION_UNIQUE_REMAINDER,
                )
            )
            matched_old.add(old_index)
            matched_new.add(new_index)
        elif remaining_old and remaining_new:
            ambiguous.append(_ambiguous(remaining_old, remaining_new))
            reserved_old.update(item[0] for item in remaining_old)
            reserved_new.update(item[0] for item in remaining_new)

    resolved_old = matched_old | reserved_old
    resolved_new = matched_new | reserved_new
    deleted = tuple(
        entry for index, entry in active_old if index not in resolved_old
    )
    reserved_uuids = {entry.uuid for entry in old_entries}
    created_list = []
    for index, projection in enumerate(new_projections):
        if index in resolved_new:
            continue
        new_uuid = str(uuid_factory())
        if not new_uuid or new_uuid in reserved_uuids:
            raise ValueError("UUID factory returned a duplicate identity UUID")
        reserved_uuids.add(new_uuid)
        created_list.append(
            IdentityRegistryEntry(
                uuid=new_uuid,
                projection=projection,
                display_name=projection.alias,
            )
        )
    created = tuple(created_list)
    matches.sort(key=lambda match: (match.new_projection.declaration_order, match.new_projection.alias))
    ambiguous.sort(
        key=lambda item: (
            _new_projection_sort_key(item.new[0]) if item.new else (0, "", ""),
            _old_entry_sort_key(item.old[0]) if item.old else (0, "", "", ""),
        )
    )
    return ReconciliationResult(
        matched=tuple(matches),
        created=created,
        deleted=deleted,
        ambiguous=tuple(ambiguous),
    )


def apply_reconciliation(result: ReconciliationResult) -> IdentityRegistry:
    """Build the next active registry, preserving metadata on matches.

    Deleted entries are omitted from the active registry. Callers may retain
    them separately as tombstones. Tombstones participate only when a caller
    explicitly enables resurrection for a known authority transition. An
    unresolved ambiguity is not a deletion or creation decision, so applying
    it is rejected explicitly.
    """

    if result.ambiguous:
        raise ValueError(
            "cannot apply reconciliation with unresolved ambiguous identities"
        )

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
