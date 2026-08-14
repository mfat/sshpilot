"""Isolated prototype for UUID-owned connection state.

This module is intentionally not wired into ``ConnectionRepository`` or the
public API.  It proves the production sidecar boundary independently from the
currently alias-shaped v1 state file:

* SSH connections are owned by canonical UUIDv4 identities;
* the last loader projection/evidence is persisted for restart reconciliation;
* app-owned group/root/metadata references use typed UUID references;
* non-SSH records remain in their existing protocol-local identity space; and
* stale v1 alias references are quarantined instead of silently discarded.

No secrets, network calls, SSH subprocesses, or filesystem I/O are performed.
The caller owns atomic persistence and transaction recovery.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from ...api.models.connection_store import validate_safe_metadata
from .identity_reconciliation import (
    ConnectionIdentityProjection,
    IdentityRegistry,
    IdentityRegistryEntry,
)
from .state_file import ConnectionFileState


V2_VERSION = 2
_PROJECTION_SERIALIZATION_UUID = "00000000-0000-4000-8000-000000000000"


def canonical_uuid(value: str) -> str:
    """Validate canonical lowercase hyphenated UUIDv4 text."""

    if type(value) is not str:
        raise TypeError("identity UUID must be a string")
    parts = value.split("-")
    if len(parts) != 5 or [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        raise ValueError("identity UUID must be valid UUID text")
    if any(character not in "0123456789abcdef" for character in "".join(parts)):
        raise ValueError("identity UUID must be valid UUID text")
    if parts[2][0] != "4" or parts[3][0] not in "89ab":
        raise ValueError("identity UUID must be canonical lowercase UUIDv4 text")
    return value


def new_uuid4() -> str:
    """Generate one app-owned UUID; aliases and projections are not inputs."""

    raw = bytearray(secrets.token_bytes(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    value = (
        f"{raw[0]:02x}{raw[1]:02x}{raw[2]:02x}{raw[3]:02x}-"
        f"{raw[4]:02x}{raw[5]:02x}-"
        f"{raw[6]:02x}{raw[7]:02x}-"
        f"{raw[8]:02x}{raw[9]:02x}-"
        f"{raw[10]:02x}{raw[11]:02x}{raw[12]:02x}{raw[13]:02x}{raw[14]:02x}{raw[15]:02x}"
    )
    return canonical_uuid(value)


class ReferenceKind(str, Enum):
    SSH_UUID = "ssh_uuid"
    NON_SSH_ID = "non_ssh_id"


@dataclass(frozen=True)
class ConnectionReference:
    kind: ReferenceKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReferenceKind):
            raise TypeError("connection reference kind must be an enum")
        if type(self.value) is not str or not self.value.strip():
            raise ValueError("connection reference value must be non-empty")
        if self.kind is ReferenceKind.SSH_UUID:
            canonical_uuid(self.value)

    def to_dict(self) -> Mapping[str, str]:
        return {"kind": self.kind.value, "id": self.value}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConnectionReference":
        if not isinstance(payload, Mapping):
            raise TypeError("connection reference must be an object")
        try:
            kind = ReferenceKind(payload.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ValueError("connection reference kind is invalid") from exc
        return cls(kind=kind, value=payload.get("id", ""))


def _projection_to_dict(projection: ConnectionIdentityProjection) -> Mapping[str, Any]:
    """Serialize the accepted prototype projection without public API fields."""

    wrapper = IdentityRegistry(
        entries=(
            IdentityRegistryEntry(
                uuid=_PROJECTION_SERIALIZATION_UUID,
                projection=projection,
            ),
        )
    )
    return wrapper.to_dict()["identities"][0]["projection"]


def _projection_from_dict(payload: Mapping[str, Any]) -> ConnectionIdentityProjection:
    """Reuse the frozen prototype evidence validators for v2 projections."""

    if not isinstance(payload, Mapping):
        raise TypeError("identity projection must be an object")
    wrapper = IdentityRegistry.from_dict(
        {
            "version": 1,
            "identities": [
                {"uuid": _PROJECTION_SERIALIZATION_UUID, "projection": dict(payload)}
            ],
        }
    )
    return wrapper.entries[0].projection


@dataclass(frozen=True)
class PersistedIdentity:
    uuid: str
    display_name: str
    projection: ConnectionIdentityProjection
    tombstone: bool = False
    retired_generation: Optional[int] = None

    def __post_init__(self) -> None:
        canonical_uuid(self.uuid)
        if not isinstance(self.projection, ConnectionIdentityProjection):
            raise TypeError("identity projection must be ConnectionIdentityProjection")
        if type(self.display_name) is not str:
            raise TypeError("display name must be a string")
        if type(self.tombstone) is not bool:
            raise TypeError("tombstone must be a boolean")
        if self.retired_generation is not None and (
            type(self.retired_generation) is not int or self.retired_generation < 0
        ):
            raise ValueError("retired generation must be a non-negative integer")
        if not self.tombstone and self.retired_generation is not None:
            raise ValueError("active identities cannot have a retired generation")

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "display_name": self.display_name,
            "projection": dict(_projection_to_dict(self.projection)),
            "tombstone": self.tombstone,
        }
        if self.retired_generation is not None:
            payload["retired_generation"] = self.retired_generation
        return payload

    @classmethod
    def from_dict(cls, identity_uuid: str, payload: Mapping[str, Any]) -> "PersistedIdentity":
        if not isinstance(payload, Mapping):
            raise TypeError("identity entry must be an object")
        return cls(
            uuid=identity_uuid,
            display_name=payload.get("display_name", ""),
            projection=_projection_from_dict(payload.get("projection", {})),
            tombstone=payload.get("tombstone", False),
            retired_generation=payload.get("retired_generation"),
        )


@dataclass(frozen=True)
class UuidGroupState:
    id: str
    name: str
    members: Tuple[ConnectionReference, ...] = ()
    parent_id: Optional[str] = None
    order: int = 0
    color: str = ""

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id.strip():
            raise ValueError("group id must be non-empty")
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("group name must be non-empty")
        if self.parent_id is not None and (
            type(self.parent_id) is not str or not self.parent_id.strip()
        ):
            raise ValueError("group parent id must be non-empty or null")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("group order must be non-negative")
        if type(self.color) is not str:
            raise TypeError("group color must be a string")
        if type(self.members) is not tuple:
            raise TypeError("group members must be a tuple")
        if len(set(self.members)) != len(self.members):
            raise ValueError("group members must not be duplicated")

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "order": self.order,
            "color": self.color,
            "members": [member.to_dict() for member in self.members],
        }
        if self.parent_id is not None:
            payload["parent_id"] = self.parent_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "UuidGroupState":
        if not isinstance(payload, Mapping):
            raise TypeError("group entry must be an object")
        raw_members = payload.get("members", [])
        if not isinstance(raw_members, list):
            raise TypeError("group members must be an array")
        return cls(
            id=payload.get("id", ""),
            name=payload.get("name", ""),
            members=tuple(ConnectionReference.from_dict(item) for item in raw_members),
            parent_id=payload.get("parent_id"),
            order=payload.get("order", 0),
            color=payload.get("color", ""),
        )


@dataclass(frozen=True)
class LegacyOrphan:
    """A v1 reference that was not safely attachable to a current record."""

    kind: str
    alias: str
    group_id: Optional[str] = None
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"group_member", "root_connection", "metadata"}:
            raise ValueError("legacy orphan kind is invalid")
        if type(self.alias) is not str or not self.alias:
            raise ValueError("legacy orphan alias must be non-empty")
        if self.group_id is not None and type(self.group_id) is not str:
            raise TypeError("legacy orphan group id must be a string or null")
        object.__setattr__(self, "values", validate_safe_metadata(self.values))

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "alias": self.alias,
            "values": dict(self.values),
        }
        if self.group_id is not None:
            payload["group_id"] = self.group_id
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LegacyOrphan":
        return cls(
            kind=payload.get("kind", ""),
            alias=payload.get("alias", ""),
            group_id=payload.get("group_id"),
            values=payload.get("values", {}),
        )


@dataclass(frozen=True)
class PendingAmbiguity:
    ssh_config_revision: str
    old_uuids: Tuple[str, ...]
    new_projections: Tuple[ConnectionIdentityProjection, ...]

    def __post_init__(self) -> None:
        if type(self.ssh_config_revision) is not str or not self.ssh_config_revision:
            raise ValueError("pending ambiguity needs an SSH revision")
        if type(self.old_uuids) is not tuple or len(set(self.old_uuids)) != len(self.old_uuids):
            raise ValueError("pending ambiguity UUIDs must be a unique tuple")
        for identity_uuid in self.old_uuids:
            canonical_uuid(identity_uuid)
        if type(self.new_projections) is not tuple:
            raise TypeError("pending ambiguity projections must be a tuple")
        if any(
            not isinstance(projection, ConnectionIdentityProjection)
            for projection in self.new_projections
        ):
            raise TypeError("pending ambiguity projections must be projections")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "ssh_config_revision": self.ssh_config_revision,
            "old_uuids": list(self.old_uuids),
            "new_projections": [
                dict(_projection_to_dict(projection))
                for projection in self.new_projections
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PendingAmbiguity":
        raw_projections = payload.get("new_projections", [])
        if not isinstance(raw_projections, list):
            raise TypeError("pending ambiguity projections must be an array")
        return cls(
            ssh_config_revision=payload.get("ssh_config_revision", ""),
            old_uuids=tuple(payload.get("old_uuids", [])),
            new_projections=tuple(
                _projection_from_dict(item) for item in raw_projections
            ),
        )


@dataclass(frozen=True)
class IdentityStateV2:
    """Concrete v2 sidecar model; not yet the production StateFile schema."""

    identities: Tuple[PersistedIdentity, ...] = ()
    groups: Tuple[UuidGroupState, ...] = ()
    root_connections: Tuple[ConnectionReference, ...] = ()
    metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    non_ssh_connections: Tuple[Mapping[str, Any], ...] = ()
    non_ssh_metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    legacy_orphans: Tuple[LegacyOrphan, ...] = ()
    pending_ambiguities: Tuple[PendingAmbiguity, ...] = ()
    sidecar_generation: int = 0
    last_reconciled_ssh_revision: Optional[str] = None
    observed_ssh_revision: Optional[str] = None
    version: int = V2_VERSION

    def __post_init__(self) -> None:
        if self.version != V2_VERSION:
            raise ValueError("unsupported identity state version")
        if type(self.sidecar_generation) is not int or self.sidecar_generation < 0:
            raise ValueError("sidecar generation must be non-negative")
        identities = [identity.uuid for identity in self.identities]
        if len(set(identities)) != len(identities):
            raise ValueError("identity UUIDs must be unique")
        group_ids = [group.id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("group IDs must be unique")
        ssh_ids = set(identities)
        non_ssh_ids: set[str] = set()
        for item in self.non_ssh_connections:
            if not isinstance(item, Mapping):
                raise TypeError("non-SSH connections must be objects")
            connection_id = item.get("id") or item.get("nickname")
            if type(connection_id) is not str or not connection_id.strip():
                raise ValueError("non-SSH connections need a non-empty id")
            if connection_id in non_ssh_ids:
                raise ValueError("non-SSH connection IDs must be unique")
            non_ssh_ids.add(connection_id)
        if type(self.root_connections) is not tuple:
            raise TypeError("root connections must be a tuple")
        if len(set(self.root_connections)) != len(self.root_connections):
            raise ValueError("root connection references must be unique")
        for reference in self.root_connections:
            self._validate_reference(reference, ssh_ids, non_ssh_ids)
        for group in self.groups:
            if group.parent_id is not None and group.parent_id not in set(group_ids):
                raise ValueError("group parent must reference an existing group")
            for reference in group.members:
                self._validate_reference(reference, ssh_ids, non_ssh_ids)
        for group in self.groups:
            seen: set[str] = set()
            parent = group.parent_id
            while parent is not None:
                if parent in seen or parent == group.id:
                    raise ValueError("group parent hierarchy must not contain cycles")
                seen.add(parent)
                parent = next(
                    candidate.parent_id
                    for candidate in self.groups
                    if candidate.id == parent
                )
        for identity_uuid, values in self.metadata.items():
            canonical_uuid(identity_uuid)
            if identity_uuid not in ssh_ids:
                raise ValueError("metadata must reference an existing SSH UUID")
            validate_safe_metadata(values)
        for connection_id, values in self.non_ssh_metadata.items():
            if type(connection_id) is not str or not connection_id:
                raise ValueError("non-SSH metadata IDs must be non-empty")
            if connection_id not in non_ssh_ids:
                raise ValueError("non-SSH metadata must reference an existing connection")
            validate_safe_metadata(values)
        if self.last_reconciled_ssh_revision is not None and not isinstance(
            self.last_reconciled_ssh_revision, str
        ):
            raise TypeError("SSH revision must be a string or null")
        if self.observed_ssh_revision is not None and not isinstance(
            self.observed_ssh_revision, str
        ):
            raise TypeError("observed SSH revision must be a string or null")

    @staticmethod
    def _validate_reference(
        reference: ConnectionReference,
        ssh_ids: set[str],
        non_ssh_ids: set[str],
    ) -> None:
        if not isinstance(reference, ConnectionReference):
            raise TypeError("connection references must be ConnectionReference values")
        if reference.kind is ReferenceKind.SSH_UUID:
            if reference.value not in ssh_ids:
                raise ValueError("reference points to an unknown SSH UUID")
        elif reference.value not in non_ssh_ids:
            raise ValueError("reference points to an unknown non-SSH connection")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "version": self.version,
            "sidecar_generation": self.sidecar_generation,
            "last_reconciled_ssh_revision": self.last_reconciled_ssh_revision,
            "observed_ssh_revision": self.observed_ssh_revision,
            "identities": {
                identity.uuid: identity.to_dict()
                for identity in self.identities
            },
            "groups": [group.to_dict() for group in self.groups],
            "root_connections": [
                reference.to_dict() for reference in self.root_connections
            ],
            "metadata": {
                identity_uuid: dict(values)
                for identity_uuid, values in self.metadata.items()
            },
            "non_ssh_connections": [dict(item) for item in self.non_ssh_connections],
            "non_ssh_metadata": {
                connection_id: dict(values)
                for connection_id, values in self.non_ssh_metadata.items()
            },
            "legacy_orphans": [orphan.to_dict() for orphan in self.legacy_orphans],
            "pending_ambiguities": [
                ambiguity.to_dict() for ambiguity in self.pending_ambiguities
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IdentityStateV2":
        if not isinstance(payload, Mapping):
            raise TypeError("identity state must be an object")
        if payload.get("version") != V2_VERSION:
            raise ValueError("unsupported identity state version")
        raw_identities = payload.get("identities", {})
        if not isinstance(raw_identities, Mapping):
            raise TypeError("identities must be a UUID-keyed object")
        identities = tuple(
            PersistedIdentity.from_dict(identity_uuid, entry)
            for identity_uuid, entry in raw_identities.items()
        )
        raw_groups = payload.get("groups", [])
        raw_root = payload.get("root_connections", [])
        if not isinstance(raw_groups, list) or not isinstance(raw_root, list):
            raise TypeError("groups and root connections must be arrays")
        raw_metadata = payload.get("metadata", {})
        raw_non_ssh_metadata = payload.get("non_ssh_metadata", {})
        if not isinstance(raw_metadata, Mapping) or not isinstance(
            raw_non_ssh_metadata, Mapping
        ):
            raise TypeError("metadata containers must be objects")
        raw_non_ssh = payload.get("non_ssh_connections", [])
        if not isinstance(raw_non_ssh, list):
            raise TypeError("non-SSH connections must be an array")
        raw_orphans = payload.get("legacy_orphans", [])
        raw_ambiguities = payload.get("pending_ambiguities", [])
        if not isinstance(raw_orphans, list) or not isinstance(raw_ambiguities, list):
            raise TypeError("diagnostic collections must be arrays")
        return cls(
            identities=identities,
            groups=tuple(UuidGroupState.from_dict(item) for item in raw_groups),
            root_connections=tuple(
                ConnectionReference.from_dict(item) for item in raw_root
            ),
            metadata={str(key): dict(value) for key, value in raw_metadata.items()},
            non_ssh_connections=tuple(
                dict(item) if isinstance(item, Mapping) else item
                for item in raw_non_ssh
            ),
            non_ssh_metadata={
                str(key): dict(value)
                for key, value in raw_non_ssh_metadata.items()
            },
            legacy_orphans=tuple(LegacyOrphan.from_dict(item) for item in raw_orphans),
            pending_ambiguities=tuple(
                PendingAmbiguity.from_dict(item) for item in raw_ambiguities
            ),
            sidecar_generation=payload.get("sidecar_generation", 0),
            last_reconciled_ssh_revision=payload.get("last_reconciled_ssh_revision"),
            observed_ssh_revision=payload.get("observed_ssh_revision"),
        )


@dataclass(frozen=True)
class MigrationReport:
    identities_created: int = 0
    groups_migrated: int = 0
    metadata_migrated: int = 0
    root_references_migrated: int = 0
    non_ssh_preserved: int = 0
    stale_references_quarantined: int = 0
    duplicate_references_deduplicated: int = 0


def _unique_uuid(factory: Callable[[], str], used: set[str]) -> str:
    candidate = canonical_uuid(str(factory()))
    if candidate in used:
        raise ValueError("UUID factory returned a duplicate identity UUID")
    used.add(candidate)
    return candidate


def migrate_v1_state(
    state: ConnectionFileState,
    projections: Sequence[ConnectionIdentityProjection],
    *,
    ssh_config_revision: Optional[str],
    uuid_factory: Callable[[], str] = new_uuid4,
) -> Tuple[IdentityStateV2, MigrationReport]:
    """Convert alias-keyed v1 state without mutating the v1 input.

    Current SSH projections are the only source for SSH identities. Unknown
    aliases from groups/root/metadata are retained as quarantined diagnostics,
    never guessed into a UUID. Non-SSH records and their metadata retain their
    existing protocol-local IDs until a future unified protocol identity
    migration.
    """

    if type(state) is not ConnectionFileState:
        raise TypeError("v1 state must be ConnectionFileState")
    projection_by_alias: dict[str, ConnectionIdentityProjection] = {}
    for projection in projections:
        if projection.alias in projection_by_alias:
            raise ValueError("current SSH projections contain duplicate aliases")
        projection_by_alias[projection.alias] = projection
    non_ssh_ids = {
        str(item.get("id") or item.get("nickname") or "")
        for item in state.non_ssh_connections
        if item.get("id") or item.get("nickname")
    }
    used: set[str] = set()
    identities = []
    uuid_by_alias: dict[str, str] = {}
    for projection in projections:
        identity_uuid = _unique_uuid(uuid_factory, used)
        uuid_by_alias[projection.alias] = identity_uuid
        identities.append(
            PersistedIdentity(
                uuid=identity_uuid,
                display_name=projection.alias,
                projection=projection,
            )
        )

    orphans = []
    duplicate_count = 0
    stale_count = 0

    def resolve_reference(
        alias: str,
        *,
        orphan_kind: str,
        group_id: Optional[str] = None,
    ) -> Optional[ConnectionReference]:
        nonlocal stale_count
        if alias in uuid_by_alias:
            return ConnectionReference(ReferenceKind.SSH_UUID, uuid_by_alias[alias])
        if alias in non_ssh_ids:
            return ConnectionReference(ReferenceKind.NON_SSH_ID, alias)
        stale_count += 1
        orphans.append(LegacyOrphan(orphan_kind, alias, group_id))
        return None

    groups = []
    for group in state.groups:
        members = []
        seen = set()
        for alias in group.connection_ids:
            reference = resolve_reference(
                alias,
                orphan_kind="group_member",
                group_id=group.id,
            )
            if reference is None:
                continue
            if reference in seen:
                duplicate_count += 1
                continue
            seen.add(reference)
            members.append(reference)
        groups.append(
            UuidGroupState(
                id=group.id,
                name=group.name,
                members=tuple(members),
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
            )
        )

    root = []
    seen_root = set()
    for alias in state.root_connections:
        reference = resolve_reference(alias, orphan_kind="root_connection")
        if reference is None:
            continue
        if reference in seen_root:
            duplicate_count += 1
            continue
        seen_root.add(reference)
        root.append(reference)

    metadata: dict[str, Mapping[str, Any]] = {}
    non_ssh_metadata: dict[str, Mapping[str, Any]] = {}
    for alias, values in state.metadata.items():
        if alias in uuid_by_alias:
            identity_uuid = uuid_by_alias[alias]
            metadata[identity_uuid] = validate_safe_metadata(values)
            continue
        if alias in non_ssh_ids:
            non_ssh_metadata[alias] = validate_safe_metadata(values)
            continue
        stale_count += 1
        orphans.append(LegacyOrphan("metadata", alias, values=values))

    # An existing safe metadata display_name is an explicit bootstrap name;
    # otherwise the alias is the only honest initial name.
    bootstrapped = []
    for identity in identities:
        values = metadata.get(identity.uuid, {})
        display_name = values.get("display_name")
        if type(display_name) is str and display_name:
            metadata[identity.uuid] = {
                key: value for key, value in values.items() if key != "display_name"
            }
            identity = PersistedIdentity(
                uuid=identity.uuid,
                display_name=display_name,
                projection=identity.projection,
            )
        bootstrapped.append(identity)

    result = IdentityStateV2(
        identities=tuple(bootstrapped),
        groups=tuple(groups),
        root_connections=tuple(root),
        metadata=metadata,
        non_ssh_connections=state.non_ssh_connections,
        non_ssh_metadata=non_ssh_metadata,
        legacy_orphans=tuple(orphans),
        sidecar_generation=0,
        last_reconciled_ssh_revision=ssh_config_revision,
        observed_ssh_revision=ssh_config_revision,
    )
    return result, MigrationReport(
        identities_created=len(identities),
        groups_migrated=len(groups),
        metadata_migrated=len(metadata) + len(non_ssh_metadata),
        root_references_migrated=len(root),
        non_ssh_preserved=len(state.non_ssh_connections),
        stale_references_quarantined=stale_count,
        duplicate_references_deduplicated=duplicate_count,
    )
