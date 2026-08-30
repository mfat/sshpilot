"""Domain model for UUID-owned connection state.

This module is intentionally not wired into ``ConnectionRepository`` or the
public API.  It defines the production sidecar boundary independently from the
currently alias-shaped v1 state file:

* SSH connections are owned by canonical UUIDv4 identities;
* the last loader projection/evidence is persisted for restart reconciliation;
* app-owned group/root/metadata references use typed UUID references;
* non-SSH records remain in their existing protocol-local identity space; and
* stale v1 alias references are quarantined instead of silently discarded.

No secrets, network calls, SSH subprocesses, or filesystem I/O are performed.
Filesystem persistence and recovery primitives live in ``state_file.py``.
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
IDENTITY_TRANSACTION_VERSION = 1
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


def new_transaction_id() -> str:
    """Generate an opaque transaction token, independent of identity UUIDs."""

    return secrets.token_hex(16)


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
    # The group this identity sat in when it was retired. A tombstone cannot be
    # a group member (see validate: "tombstoned identities cannot be group
    # members"), so leaving a root would otherwise discard the placement for
    # good -- the identity resurrects with its UUID and display name but at the
    # root of the tree. Remembering it here is the same bookkeeping pattern as
    # retired_generation, and it is cleared the moment the identity comes back.
    retired_group_id: Optional[str] = None

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
        if self.retired_group_id is not None and type(self.retired_group_id) is not str:
            raise TypeError("retired group id must be a string or null")
        if not self.tombstone and self.retired_group_id is not None:
            raise ValueError("active identities cannot have a retired group id")

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "display_name": self.display_name,
            "projection": dict(_projection_to_dict(self.projection)),
            "tombstone": self.tombstone,
        }
        if self.retired_generation is not None:
            payload["retired_generation"] = self.retired_generation
        if self.retired_group_id is not None:
            payload["retired_group_id"] = self.retired_group_id
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
            retired_group_id=payload.get("retired_group_id"),
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
    original_index: Optional[int] = None
    original_container_length: Optional[int] = None
    reason: str = "stale_reference"

    def __post_init__(self) -> None:
        if self.kind not in {"group_member", "root_connection", "metadata"}:
            raise ValueError("legacy orphan kind is invalid")
        if type(self.alias) is not str or not self.alias:
            raise ValueError("legacy orphan alias must be non-empty")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("legacy orphan reason must be non-empty")
        if self.kind == "group_member":
            if type(self.group_id) is not str or not self.group_id.strip():
                raise ValueError("group-member orphan needs a non-empty group id")
            self._validate_position()
        elif self.kind == "root_connection":
            if self.group_id is not None:
                raise ValueError("root orphan cannot have a group id")
            self._validate_position()
        elif self.group_id is not None:
            raise ValueError("metadata orphan cannot have a group id")
        elif self.original_index is not None or self.original_container_length is not None:
            raise ValueError("metadata orphan cannot have placement coordinates")
        if not isinstance(self.values, Mapping):
            raise TypeError("legacy orphan values must be an object")
        object.__setattr__(self, "values", validate_safe_metadata(self.values))

    def _validate_position(self) -> None:
        if type(self.original_index) is not int or self.original_index < 0:
            raise ValueError("placement orphan needs a non-negative original index")
        if type(self.original_container_length) is not int or (
            self.original_container_length <= 0
        ):
            raise ValueError("placement orphan needs a positive container length")
        if self.original_index >= self.original_container_length:
            raise ValueError("orphan index must be inside its container")

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "alias": self.alias,
            "values": dict(self.values),
            "reason": self.reason,
        }
        if self.group_id is not None:
            payload["group_id"] = self.group_id
        if self.original_index is not None:
            payload["original_index"] = self.original_index
        if self.original_container_length is not None:
            payload["original_container_length"] = self.original_container_length
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LegacyOrphan":
        if not isinstance(payload, Mapping):
            raise TypeError("legacy orphan must be an object")
        values = payload.get("values", {})
        if not isinstance(values, Mapping):
            raise TypeError("legacy orphan values must be an object")
        return cls(
            kind=payload.get("kind", ""),
            alias=payload.get("alias", ""),
            group_id=payload.get("group_id"),
            values=values,
            original_index=payload.get("original_index"),
            original_container_length=payload.get("original_container_length"),
            reason=payload.get("reason", "stale_reference"),
        )


@dataclass(frozen=True)
class PendingAmbiguity:
    ssh_config_revision: str
    old_uuids: Tuple[str, ...]
    new_projections: Tuple[ConnectionIdentityProjection, ...]

    def __post_init__(self) -> None:
        if type(self.ssh_config_revision) is not str or not self.ssh_config_revision:
            raise ValueError("pending ambiguity needs an SSH revision")
        if type(self.old_uuids) is not tuple or not self.old_uuids:
            raise ValueError("pending ambiguity needs old UUID candidates")
        if len(set(self.old_uuids)) != len(self.old_uuids):
            raise ValueError("pending ambiguity UUIDs must be a unique tuple")
        for identity_uuid in self.old_uuids:
            canonical_uuid(identity_uuid)
        if type(self.new_projections) is not tuple or not self.new_projections:
            raise ValueError("pending ambiguity needs new projections")
        if any(
            not isinstance(projection, ConnectionIdentityProjection)
            for projection in self.new_projections
        ):
            raise TypeError("pending ambiguity projections must be projections")
        aliases = [projection.alias for projection in self.new_projections]
        if len(aliases) != len(set(aliases)):
            raise ValueError("pending ambiguity aliases must be unique")

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
        if not isinstance(payload, Mapping):
            raise TypeError("pending ambiguity must be an object")
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
class AmbiguityResolution:
    """Ephemeral, revision-guarded backend resolution request.

    This is intentionally not a public API DTO or a durable journal. It
    validates the complete user decision before a future production adapter
    constructs and commits a new ``IdentityStateV2`` snapshot.
    """

    expected_ssh_revision: str
    expected_sidecar_generation: int
    old_to_new: Tuple[Tuple[str, str], ...] = ()
    explicit_creates: Tuple[str, ...] = ()
    explicit_deletes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.expected_ssh_revision) is not str or not self.expected_ssh_revision:
            raise ValueError("ambiguity resolution needs an SSH revision")
        if type(self.expected_sidecar_generation) is not int or (
            self.expected_sidecar_generation < 0
        ):
            raise ValueError("ambiguity resolution needs a valid sidecar generation")
        if type(self.old_to_new) is not tuple:
            raise TypeError("ambiguity mappings must be a tuple")
        old_uuids = [old_uuid for old_uuid, _ in self.old_to_new]
        new_aliases = [new_alias for _, new_alias in self.old_to_new]
        if len(old_uuids) != len(set(old_uuids)):
            raise ValueError("an old UUID may be mapped once")
        if len(new_aliases) != len(set(new_aliases)):
            raise ValueError("a new alias may be mapped once")
        for old_uuid, new_alias in self.old_to_new:
            canonical_uuid(old_uuid)
            if type(new_alias) is not str or not new_alias:
                raise ValueError("mapped aliases must be non-empty strings")
        for aliases, label in (
            (self.explicit_creates, "created aliases"),
            (self.explicit_deletes, "deleted UUIDs"),
        ):
            if type(aliases) is not tuple:
                raise TypeError(f"{label} must be a tuple")
            if len(aliases) != len(set(aliases)):
                raise ValueError(f"{label} must be unique")
        for alias in self.explicit_creates:
            if type(alias) is not str or not alias:
                raise ValueError("created aliases must be non-empty strings")
        for identity_uuid in self.explicit_deletes:
            canonical_uuid(identity_uuid)
        if set(old_uuids) & set(self.explicit_deletes):
            raise ValueError("an old UUID cannot be mapped and explicitly deleted")
        if set(new_aliases) & set(self.explicit_creates):
            raise ValueError("an alias cannot be mapped and explicitly created")


def validate_ambiguity_resolution(
    state: "IdentityStateV2",
    resolution: AmbiguityResolution,
) -> PendingAmbiguity:
    """Validate a complete decision against one current pending ambiguity.

    The function has no mutation or persistence side effect. A production
    adapter can use the returned ambiguity to build a new validated snapshot,
    increment the sidecar generation, and clear that ambiguity.
    """

    if not isinstance(state, IdentityStateV2):
        raise TypeError("ambiguity resolution needs IdentityStateV2")
    if not isinstance(resolution, AmbiguityResolution):
        raise TypeError("ambiguity resolution has an invalid type")
    if state.observed_ssh_revision != resolution.expected_ssh_revision:
        raise ValueError("stale SSH revision for ambiguity resolution")
    if state.sidecar_generation != resolution.expected_sidecar_generation:
        raise ValueError("stale sidecar generation for ambiguity resolution")
    mapped_old = {old_uuid for old_uuid, _ in resolution.old_to_new}
    mapped_new = {new_alias for _, new_alias in resolution.old_to_new}
    requested_old = mapped_old | set(resolution.explicit_deletes)
    requested_new = mapped_new | set(resolution.explicit_creates)
    candidates = [
        ambiguity
        for ambiguity in state.pending_ambiguities
        if ambiguity.ssh_config_revision == resolution.expected_ssh_revision
        and requested_old == set(ambiguity.old_uuids)
        and requested_new == {item.alias for item in ambiguity.new_projections}
    ]
    if len(candidates) != 1:
        raise ValueError("resolution must account for exactly one pending ambiguity")
    return candidates[0]


@dataclass(frozen=True)
class IdentityStateV2:
    """Concrete validated v2 sidecar snapshot."""

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
        if type(self.identities) is not tuple or any(
            not isinstance(identity, PersistedIdentity) for identity in self.identities
        ):
            raise TypeError("identities must be PersistedIdentity values")
        identities = [identity.uuid for identity in self.identities]
        if len(set(identities)) != len(identities):
            raise ValueError("identity UUIDs must be unique")
        identity_by_uuid = {identity.uuid: identity for identity in self.identities}
        active_ssh_ids = {
            identity_uuid
            for identity_uuid, identity in identity_by_uuid.items()
            if not identity.tombstone
        }
        if type(self.groups) is not tuple or any(
            not isinstance(group, UuidGroupState) for group in self.groups
        ):
            raise TypeError("groups must be UuidGroupState values")
        group_ids = [group.id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("group IDs must be unique")
        ssh_ids = set(identities)
        if type(self.non_ssh_connections) is not tuple:
            raise TypeError("non-SSH connections must be a tuple")
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
        for reference in self.root_connections:
            self._validate_reference(reference, ssh_ids, non_ssh_ids)
            if reference.kind is ReferenceKind.SSH_UUID and identity_by_uuid[
                reference.value
            ].tombstone:
                raise ValueError("tombstoned identities cannot be root connections")
        if len(set(self.root_connections)) != len(self.root_connections):
            raise ValueError("root connection references must be unique")
        grouped_references: set[ConnectionReference] = set()
        for group in self.groups:
            if group.parent_id is not None and group.parent_id not in set(group_ids):
                raise ValueError("group parent must reference an existing group")
            for reference in group.members:
                self._validate_reference(reference, ssh_ids, non_ssh_ids)
                grouped_references.add(reference)
                if reference.kind is ReferenceKind.SSH_UUID and identity_by_uuid[
                    reference.value
                ].tombstone:
                    raise ValueError("tombstoned identities cannot be group members")
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
        root_references = set(self.root_connections)
        all_active_references = {
            ConnectionReference(ReferenceKind.SSH_UUID, identity_uuid)
            for identity_uuid in active_ssh_ids
        } | {
            ConnectionReference(ReferenceKind.NON_SSH_ID, connection_id)
            for connection_id in non_ssh_ids
        }
        if grouped_references & root_references:
            raise ValueError("grouped connections cannot also be root connections")
        if all_active_references - grouped_references - root_references:
            raise ValueError("every ungrouped active connection needs root placement")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        for identity_uuid, values in self.metadata.items():
            if type(identity_uuid) is not str:
                raise TypeError("metadata UUID keys must be strings")
            canonical_uuid(identity_uuid)
            if identity_uuid not in ssh_ids:
                raise ValueError("metadata must reference an existing SSH UUID")
            if not isinstance(values, Mapping):
                raise TypeError("metadata values must be objects")
            validate_safe_metadata(values)
        if not isinstance(self.non_ssh_metadata, Mapping):
            raise TypeError("non-SSH metadata must be a mapping")
        for connection_id, values in self.non_ssh_metadata.items():
            if type(connection_id) is not str or not connection_id:
                raise ValueError("non-SSH metadata IDs must be non-empty")
            if connection_id not in non_ssh_ids:
                raise ValueError("non-SSH metadata must reference an existing connection")
            if not isinstance(values, Mapping):
                raise TypeError("non-SSH metadata values must be objects")
            validate_safe_metadata(values)
        if type(self.pending_ambiguities) is not tuple or any(
            not isinstance(item, PendingAmbiguity)
            for item in self.pending_ambiguities
        ):
            raise TypeError("pending ambiguities must be PendingAmbiguity values")
        pending_old: set[str] = set()
        pending_aliases: set[str] = set()
        active_aliases = {
            identity.projection.alias
            for identity in self.identities
            if not identity.tombstone
        }
        for ambiguity in self.pending_ambiguities:
            for identity_uuid in ambiguity.old_uuids:
                if identity_uuid not in active_ssh_ids:
                    raise ValueError(
                        "pending ambiguity must reference an active SSH UUID"
                    )
                if identity_uuid in pending_old:
                    raise ValueError("an old UUID may appear in one ambiguity only")
                pending_old.add(identity_uuid)
            for projection in ambiguity.new_projections:
                if projection.alias in pending_aliases:
                    raise ValueError(
                        "an unresolved alias may appear in one ambiguity only"
                    )
                if projection.alias in active_aliases:
                    raise ValueError(
                        "pending ambiguity alias already belongs to an active identity"
                    )
                pending_aliases.add(projection.alias)
        self._validate_optional_revision(
            self.last_reconciled_ssh_revision,
            "last-reconciled SSH revision",
        )
        self._validate_optional_revision(
            self.observed_ssh_revision,
            "observed SSH revision",
        )
        if self.pending_ambiguities:
            if not self.observed_ssh_revision:
                raise ValueError(
                    "pending ambiguities require an observed SSH revision"
                )
            for ambiguity in self.pending_ambiguities:
                if ambiguity.ssh_config_revision != self.observed_ssh_revision:
                    raise ValueError(
                        "pending ambiguity revision must equal observed SSH revision"
                    )

    @staticmethod
    def _validate_optional_revision(value: Optional[str], label: str) -> None:
        if value is None:
            return
        if type(value) is not str:
            raise TypeError(f"{label} must be a non-empty string or null")
        if not value:
            raise ValueError(f"{label} must be a non-empty string or null")

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
        required = {
            "version",
            "sidecar_generation",
            "last_reconciled_ssh_revision",
            "observed_ssh_revision",
            "identities",
            "groups",
            "root_connections",
            "metadata",
            "non_ssh_connections",
            "non_ssh_metadata",
            "legacy_orphans",
            "pending_ambiguities",
        }
        missing = required - set(payload)
        if missing:
            raise ValueError("identity state is missing canonical fields")
        raw_identities = payload.get("identities", {})
        if not isinstance(raw_identities, Mapping):
            raise TypeError("identities must be a UUID-keyed object")
        if any(not isinstance(entry, Mapping) for entry in raw_identities.values()):
            raise TypeError("identity entries must be objects")
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
        if any(
            type(key) is not str or not isinstance(value, Mapping)
            for key, value in raw_metadata.items()
        ):
            raise TypeError("metadata values must be objects with string keys")
        if any(
            type(key) is not str or not isinstance(value, Mapping)
            for key, value in raw_non_ssh_metadata.items()
        ):
            raise TypeError("non-SSH metadata values must be objects with string keys")
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
            metadata={key: dict(value) for key, value in raw_metadata.items()},
            non_ssh_connections=tuple(
                dict(item) if isinstance(item, Mapping) else item
                for item in raw_non_ssh
            ),
            non_ssh_metadata={
                key: dict(value) for key, value in raw_non_ssh_metadata.items()
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
class IdentityTransactionIntent:
    """Durable intent containing a complete validated v2 target snapshot."""

    transaction_id: str
    base_ssh_revision: str
    target_ssh_revision: str
    base_sidecar_generation: int
    operation_label: str
    target_state: IdentityStateV2
    operation_kind: str = "normal"
    version: int = IDENTITY_TRANSACTION_VERSION

    def __post_init__(self) -> None:
        if self.version != IDENTITY_TRANSACTION_VERSION:
            raise ValueError("unsupported identity transaction version")
        for value, label in (
            (self.transaction_id, "transaction id"),
            (self.base_ssh_revision, "base SSH revision"),
            (self.target_ssh_revision, "target SSH revision"),
            (self.operation_label, "operation label"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")
        if type(self.base_sidecar_generation) is not int or (
            self.base_sidecar_generation < 0
        ):
            raise ValueError("base sidecar generation must be non-negative")
        if not isinstance(self.target_state, IdentityStateV2):
            raise TypeError("transaction target must be IdentityStateV2")
        if self.target_state.sidecar_generation != self.base_sidecar_generation + 1:
            raise ValueError(
                "transaction target generation must be base generation plus one"
            )
        if self.operation_kind not in {"normal", "pending_ambiguity"}:
            raise ValueError("identity transaction kind is invalid")
        if self.operation_kind == "normal":
            if (
                self.target_state.last_reconciled_ssh_revision
                != self.target_ssh_revision
                or self.target_state.observed_ssh_revision
                != self.target_ssh_revision
            ):
                raise ValueError(
                    "normal transaction target must record its target SSH revision"
                )
            if self.target_state.pending_ambiguities:
                raise ValueError(
                    "normal transaction target cannot contain pending ambiguities"
                )
        else:
            if not self.target_state.pending_ambiguities:
                raise ValueError(
                    "pending-ambiguity target needs pending ambiguities"
                )
            if self.target_state.observed_ssh_revision != self.target_ssh_revision:
                raise ValueError(
                    "pending-ambiguity target must observe its target revision"
                )

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "version": self.version,
            "transaction_id": self.transaction_id,
            "base_ssh_revision": self.base_ssh_revision,
            "target_ssh_revision": self.target_ssh_revision,
            "base_sidecar_generation": self.base_sidecar_generation,
            "operation_label": self.operation_label,
            "operation_kind": self.operation_kind,
            "target_state": self.target_state.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IdentityTransactionIntent":
        if not isinstance(payload, Mapping):
            raise TypeError("identity transaction must be an object")
        required = {
            "version",
            "transaction_id",
            "base_ssh_revision",
            "target_ssh_revision",
            "base_sidecar_generation",
            "operation_label",
            "operation_kind",
            "target_state",
        }
        if required - set(payload):
            raise ValueError("identity transaction is missing canonical fields")
        target = payload.get("target_state")
        if not isinstance(target, Mapping):
            raise TypeError("transaction target must be an object")
        return cls(
            version=payload["version"],
            transaction_id=payload["transaction_id"],
            base_ssh_revision=payload["base_ssh_revision"],
            target_ssh_revision=payload["target_ssh_revision"],
            base_sidecar_generation=payload["base_sidecar_generation"],
            operation_label=payload["operation_label"],
            operation_kind=payload["operation_kind"],
            target_state=IdentityStateV2.from_dict(target),
        )


class IdentityRecoveryAction(str, Enum):
    NO_PENDING = "no_pending"
    FINALIZE_TARGET = "finalize_target"
    ABORT_BASE = "abort_base"
    REQUIRES_RECONCILIATION = "requires_reconciliation"
    DEFERRED = "deferred"
    STALE_INTENT = "stale_intent"


@dataclass(frozen=True)
class IdentityRecoveryDecision:
    action: IdentityRecoveryAction
    reason: str


def classify_identity_transaction_recovery(
    current_state: IdentityStateV2,
    intent: Optional[IdentityTransactionIntent],
    actual_ssh_revision: Optional[str],
) -> IdentityRecoveryDecision:
    """Classify recovery without reading or mutating the filesystem."""

    if not isinstance(current_state, IdentityStateV2):
        raise TypeError("recovery needs the current IdentityStateV2")
    if intent is None:
        return IdentityRecoveryDecision(
            IdentityRecoveryAction.NO_PENDING, "no pending identity transaction"
        )
    if not isinstance(intent, IdentityTransactionIntent):
        raise TypeError("recovery intent has an invalid type")
    if actual_ssh_revision is None or (
        type(actual_ssh_revision) is not str or not actual_ssh_revision
    ):
        return IdentityRecoveryDecision(
            IdentityRecoveryAction.DEFERRED,
            "complete SSH revision is unavailable",
        )

    current_is_target = current_state == intent.target_state
    if actual_ssh_revision == intent.target_ssh_revision:
        if current_is_target or current_state.sidecar_generation == (
            intent.base_sidecar_generation
        ):
            return IdentityRecoveryDecision(
                IdentityRecoveryAction.FINALIZE_TARGET,
                "SSH config matches the transaction target revision",
            )
        return IdentityRecoveryDecision(
            IdentityRecoveryAction.STALE_INTENT,
            "target revision conflicts with a newer sidecar state",
        )

    if actual_ssh_revision == intent.base_ssh_revision:
        if current_state.sidecar_generation == intent.base_sidecar_generation:
            return IdentityRecoveryDecision(
                IdentityRecoveryAction.ABORT_BASE,
                "SSH config remains at the transaction base revision",
            )
        return IdentityRecoveryDecision(
            IdentityRecoveryAction.STALE_INTENT,
            "base revision conflicts with a newer sidecar state",
        )

    if current_state.sidecar_generation == intent.base_sidecar_generation:
        return IdentityRecoveryDecision(
            IdentityRecoveryAction.REQUIRES_RECONCILIATION,
            "SSH config has an unrelated revision",
        )
    return IdentityRecoveryDecision(
        IdentityRecoveryAction.STALE_INTENT,
        "pending transaction is stale relative to the sidecar",
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
    placement_conflicts_resolved: int = 0
    unplaced_connections_appended: int = 0
    namespace_collisions_quarantined: int = 0


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
    non_ssh_ids = []
    for item in state.non_ssh_connections:
        connection_id = item.get("id") or item.get("nickname")
        if connection_id:
            if connection_id in non_ssh_ids:
                raise ValueError("non-SSH connection IDs must be unique")
            non_ssh_ids.append(connection_id)
    non_ssh_id_set = set(non_ssh_ids)
    namespace_collisions = set(projection_by_alias) & non_ssh_id_set
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

    def quarantine(
        *,
        kind: str,
        alias: str,
        group_id: Optional[str],
        original_index: Optional[int],
        original_container_length: Optional[int],
        reason: str,
        values: Optional[Mapping[str, Any]] = None,
    ) -> None:
        nonlocal stale_count
        stale_count += 1
        orphans.append(
            LegacyOrphan(
                kind=kind,
                alias=alias,
                group_id=group_id,
                values=values or {},
                original_index=original_index,
                original_container_length=original_container_length,
                reason=reason,
            )
        )

    def resolve_reference(
        alias: str,
        *,
        orphan_kind: str,
        group_id: Optional[str] = None,
        original_index: Optional[int],
        original_container_length: Optional[int],
        values: Optional[Mapping[str, Any]] = None,
    ) -> Optional[ConnectionReference]:
        if alias in namespace_collisions:
            quarantine(
                kind=orphan_kind,
                alias=alias,
                group_id=group_id,
                original_index=original_index,
                original_container_length=original_container_length,
                reason="namespace_collision",
                values=values,
            )
            return None
        if alias in uuid_by_alias:
            return ConnectionReference(ReferenceKind.SSH_UUID, uuid_by_alias[alias])
        if alias in non_ssh_id_set:
            return ConnectionReference(ReferenceKind.NON_SSH_ID, alias)
        quarantine(
            kind=orphan_kind,
            alias=alias,
            group_id=group_id,
            original_index=original_index,
            original_container_length=original_container_length,
            reason="stale_reference",
            values=values,
        )
        return None

    groups = []
    for group in state.groups:
        members = []
        seen = set()
        for index, alias in enumerate(group.connection_ids):
            reference = resolve_reference(
                alias,
                orphan_kind="group_member",
                group_id=group.id,
                original_index=index,
                original_container_length=len(group.connection_ids),
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
    grouped_references = {member for group in groups for member in group.members}
    placement_conflicts = 0
    for index, alias in enumerate(state.root_connections):
        reference = resolve_reference(
            alias,
            orphan_kind="root_connection",
            original_index=index,
            original_container_length=len(state.root_connections),
        )
        if reference is None:
            continue
        if reference in grouped_references:
            placement_conflicts += 1
            duplicate_count += 1
            continue
        if reference in seen_root:
            duplicate_count += 1
            continue
        seen_root.add(reference)
        root.append(reference)

    metadata: dict[str, Mapping[str, Any]] = {}
    non_ssh_metadata: dict[str, Mapping[str, Any]] = {}
    for alias, values in state.metadata.items():
        if alias in namespace_collisions:
            quarantine(
                kind="metadata",
                alias=alias,
                group_id=None,
                original_index=None,
                original_container_length=None,
                reason="namespace_collision",
                values=values,
            )
            continue
        if alias in uuid_by_alias:
            identity_uuid = uuid_by_alias[alias]
            metadata[identity_uuid] = validate_safe_metadata(values)
            continue
        if alias in non_ssh_id_set:
            non_ssh_metadata[alias] = validate_safe_metadata(values)
            continue
        quarantine(
            kind="metadata",
            alias=alias,
            group_id=None,
            original_index=None,
            original_container_length=None,
            reason="stale_reference",
            values=values,
        )

    grouped = grouped_references
    root_set = set(root)
    active_references = [
        ConnectionReference(ReferenceKind.SSH_UUID, uuid_by_alias[projection.alias])
        for projection in projections
    ] + [
        ConnectionReference(ReferenceKind.NON_SSH_ID, connection_id)
        for connection_id in non_ssh_ids
    ]
    unplaced = [
        reference
        for reference in active_references
        if reference not in grouped and reference not in root_set
    ]
    root.extend(unplaced)

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
        placement_conflicts_resolved=placement_conflicts,
        unplaced_connections_appended=len(unplaced),
        namespace_collisions_quarantined=len(namespace_collisions),
    )
