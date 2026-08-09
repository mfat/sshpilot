"""Frontend-neutral connection-store protocol models.

These models describe the daemon-owned connection store: SSH + non-SSH
connections, connection groups and ordering, and safe per-connection metadata.
The frontend receives immutable snapshots and never treats client-supplied
paths as authority. Group expansion is deliberately absent because it is
visual frontend state.

These models are part of the published frontend-neutral API schema.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, NewType, Optional, Tuple

from .common import ConnectionId, require_identifier

if TYPE_CHECKING:
    from .connections import ConnectionSummary

GroupId = NewType("GroupId", str)

_SECRET_KEY_FRAGMENTS = (
    "password",
    "passphrase",
    "secret",
    "token",
    "credential",
    "cookie",
    "private_key",
)

_MAX_METADATA_DEPTH = 8
_MAX_METADATA_BYTES = 256 * 1024


class _FrozenDict(dict):
    """Read-only dict subclass that remains compatible with DTO validation."""

    def _readonly(self, *args, **kwargs):
        raise TypeError("metadata mappings are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _readonly

    def __ior__(self, other):
        self._readonly(other)


def _looks_like_secret_key(key: str) -> bool:
    """True when a metadata key contains a secret-like fragment."""
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _validate_metadata_node(value: Any, depth: int) -> None:
    """Validate one metadata value node for JSON safety, depth, and NUL."""
    if depth > _MAX_METADATA_DEPTH:
        raise ValueError("connection metadata exceeds maximum nesting depth")
    if value is None or type(value) is str or type(value) is bool or type(value) is int:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("connection metadata values must not contain NUL")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("connection metadata floats must be finite")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_metadata_node(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("connection metadata keys must be strings")
            if "\x00" in key:
                raise ValueError("connection metadata keys must not contain NUL")
            if _looks_like_secret_key(key):
                raise ValueError(
                    "connection metadata keys must not contain secret-like names"
                )
            _validate_metadata_node(item, depth + 1)
        return
    raise TypeError("connection metadata contains a non-JSON-safe value")


def thaw_safe_metadata(value: Any) -> Any:
    """Convert immutable metadata values into ordinary JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw_safe_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_safe_metadata(item) for item in value]
    return copy.deepcopy(value)


def validate_safe_metadata(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and deep-copy a safe connection-metadata mapping.

    Accepts JSON-safe values only (None, exact str/bool/int, finite float,
    list/tuple, dict with string keys), enforces a maximum nesting depth and a
    maximum encoded size, and rejects NUL characters and secret-like keys.
    Returns a fresh deep copy so callers cannot mutate stored state.
    """
    if not isinstance(mapping, Mapping):
        raise TypeError("metadata must be a mapping")
    copied = thaw_safe_metadata(mapping)
    for key in copied:
        if type(key) is not str:
            raise TypeError("connection metadata keys must be strings")
        if "\x00" in key:
            raise ValueError("connection metadata keys must not contain NUL")
        if _looks_like_secret_key(key):
            raise ValueError(
                "connection metadata keys must not contain secret-like names"
            )
        if isinstance(copied[key], str) and "\x00" in copied[key]:
            raise ValueError("connection metadata values must not contain NUL")
    _validate_metadata_node(copied, 0)
    encoded = len(
        json.dumps(copied, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if encoded > _MAX_METADATA_BYTES:
        raise ValueError("connection metadata exceeds the size limit")
    def freeze(value: Any) -> Any:
        if isinstance(value, Mapping):
            return _FrozenDict({key: freeze(item) for key, item in value.items()})
        if isinstance(value, (list, tuple)):
            return tuple(freeze(item) for item in value)
        return value

    return freeze(copied)


@dataclass(frozen=True)
class GroupSummary:
    """One connection group in the daemon-owned store.

    Group expansion is deliberately absent: it is visual frontend state.
    """

    id: GroupId
    name: str
    parent_id: Optional[GroupId] = None
    order: int = 0
    color: str = ""
    connection_ids: Tuple[ConnectionId, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.id, "group id")
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("group name must be a non-empty string")
        for field_name in ("name", "color"):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a string")
            if "\x00" in value:
                raise ValueError(f"{field_name} must not contain NUL")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("group order must be a non-negative integer")
        if type(self.connection_ids) is not tuple:
            raise TypeError("group connection_ids must be a tuple")
        for cid in self.connection_ids:
            if type(cid) is not str or not cid.strip():
                raise ValueError("group connection ids must be non-empty strings")
        seen = set()
        for cid in self.connection_ids:
            if cid in seen:
                raise ValueError("group connection ids must not contain duplicates")
            seen.add(cid)
        if self.parent_id == self.id:
            raise ValueError("a group cannot be its own parent")


@dataclass(frozen=True)
class ConnectionMetadataSummary:
    """Safe per-connection metadata (tags, pinning, WoL). Values never repr'd."""

    connection_id: ConnectionId
    values: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        object.__setattr__(
            self, "values", validate_safe_metadata(self.values)
        )


@dataclass(frozen=True)
class ConnectionStoreSnapshot:
    """The complete daemon-owned connection store, revisioned by generation."""

    generation: int
    connections: Tuple["ConnectionSummary", ...]
    groups: Tuple[GroupSummary, ...]
    root_connection_ids: Tuple[ConnectionId, ...]
    metadata: Tuple[ConnectionMetadataSummary, ...]

    def __post_init__(self) -> None:
        from .connections import ConnectionSummary  # deferred: avoid import cycle

        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("store generation must be a non-negative integer")
        for name, collection, expected in (
            ("connections", self.connections, ConnectionSummary),
            ("groups", self.groups, GroupSummary),
            ("root_connection_ids", self.root_connection_ids, str),
            ("metadata", self.metadata, ConnectionMetadataSummary),
        ):
            if type(collection) is not tuple:
                raise TypeError(f"store {name} must be a tuple")
            for member in collection:
                if type(member) is not expected:
                    raise TypeError(f"store {name} contains a member of the wrong type")

        connection_ids = [c.id for c in self.connections]
        if len(set(connection_ids)) != len(connection_ids):
            raise ValueError("store connections must not contain duplicate ids")
        connection_set = set(connection_ids)

        group_ids = [g.id for g in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("store groups must not contain duplicate ids")
        group_set = set(group_ids)

        metadata_ids = [m.connection_id for m in self.metadata]
        if len(set(metadata_ids)) != len(metadata_ids):
            raise ValueError("store metadata must not contain duplicate connection ids")
        for mid in metadata_ids:
            if mid not in connection_set:
                raise ValueError("store metadata refers to an unknown connection")

        grouped: set = set()
        for group in self.groups:
            if group.parent_id is not None and group.parent_id not in group_set:
                raise ValueError("store group parent refers to an unknown group")
            for cid in group.connection_ids:
                if cid not in connection_set:
                    raise ValueError("store group refers to an unknown connection")
                grouped.add(cid)

        # Parent-cycle detection (a group must never be its own ancestor).
        for group in self.groups:
            seen = set()
            current = group.parent_id
            while current is not None:
                if current in seen:
                    raise ValueError("store groups must not contain parent cycles")
                seen.add(current)
                parent = next((g for g in self.groups if g.id == current), None)
                if parent is None:
                    break
                current = parent.parent_id

        if len(set(self.root_connection_ids)) != len(self.root_connection_ids):
            raise ValueError("store root connection ids must not contain duplicates")
        for cid in self.root_connection_ids:
            if cid not in connection_set:
                raise ValueError("store root connection refers to an unknown connection")
            if cid in grouped:
                raise ValueError("a root connection must not belong to a group")
        for cid in connection_ids:
            if cid not in grouped and cid not in self.root_connection_ids:
                raise ValueError(
                    "every ungrouped connection must appear exactly once in root ids"
                )


# -- Group mutation requests ------------------------------------------------

@dataclass(frozen=True)
class SetGroupColorRequest:
    """Set a group's display color."""

    group_id: GroupId
    color: str

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group id")
        if type(self.color) is not str:
            raise TypeError("group color must be a string")
        if "\x00" in self.color:
            raise ValueError("group color must not contain NUL")


@dataclass(frozen=True)
class PlaceGroupRequest:
    """Place a group at a parent/index in the sibling order."""

    group_id: GroupId
    parent_id: Optional[GroupId]
    index: int
    expected_generation: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group id")
        if self.parent_id is not None and not str(self.parent_id).strip():
            raise ValueError("group parent id must not be empty")
        if type(self.index) is not int or self.index < 0:
            raise ValueError("group placement index must be a non-negative integer")
        if self.expected_generation is not None and (
            type(self.expected_generation) is not int
            or self.expected_generation < 0
        ):
            raise ValueError("expected generation must be a non-negative integer")


@dataclass(frozen=True)
class CopyConnectionToGroupRequest:
    """Copy a connection into a group, preserving existing memberships."""

    connection_id: ConnectionId
    group_id: GroupId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.group_id, "group id")


@dataclass(frozen=True)
class RemoveConnectionFromGroupRequest:
    """Remove one membership of a connection from a group."""

    connection_id: ConnectionId
    group_id: GroupId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.group_id, "group id")


@dataclass(frozen=True)
class ReorderConnectionRequest:
    """Reorder a connection above/below a target within a group or the root."""

    connection_id: ConnectionId
    target_connection_id: ConnectionId
    group_id: Optional[GroupId] = None
    position: str = "below"

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.target_connection_id, "target connection id")
        if self.connection_id == self.target_connection_id:
            raise ValueError("a connection cannot be reordered against itself")
        if self.group_id is not None and not str(self.group_id).strip():
            raise ValueError("group id must not be empty")
        if self.position not in ("above", "below"):
            raise ValueError("reorder position must be 'above' or 'below'")


class ConnectionPlacementMode(str, Enum):
    """Membership semantics for an atomic connection placement."""

    EXCLUSIVE = "exclusive"
    PRESERVE = "preserve"
    ADDITIVE = "additive"


@dataclass(frozen=True)
class MoveConnectionsRequest:
    """Place one or more connections with explicit membership semantics."""

    connection_ids: Tuple[ConnectionId, ...]
    target_group_id: Optional[GroupId] = None
    target_connection_id: Optional[ConnectionId] = None
    position: Optional[str] = None
    expected_generation: Optional[int] = None
    source_group_id: Optional[GroupId] = None
    mode: ConnectionPlacementMode = ConnectionPlacementMode.EXCLUSIVE

    def __post_init__(self) -> None:
        if type(self.connection_ids) is not tuple or not self.connection_ids:
            raise ValueError("connection ids must be a non-empty tuple")
        for connection_id in self.connection_ids:
            require_identifier(connection_id, "connection id")
        if len(set(self.connection_ids)) != len(self.connection_ids):
            raise ValueError("connection ids must be unique")
        if self.target_group_id is not None:
            require_identifier(self.target_group_id, "target group id")
        if self.source_group_id is not None:
            require_identifier(self.source_group_id, "source group id")
        if not isinstance(self.mode, ConnectionPlacementMode):
            try:
                object.__setattr__(self, "mode", ConnectionPlacementMode(self.mode))
            except (TypeError, ValueError) as error:
                raise ValueError("invalid connection placement mode") from error
        if self.target_connection_id is not None:
            require_identifier(self.target_connection_id, "target connection id")
            if self.target_connection_id in self.connection_ids:
                raise ValueError("target connection must not be moved")
        if self.position not in (None, "above", "below"):
            raise ValueError("move position must be 'above', 'below', or None")
        if self.target_connection_id is None and self.position is not None:
            raise ValueError("move position requires a target connection")
        if self.mode is ConnectionPlacementMode.ADDITIVE and self.target_group_id is None:
            raise ValueError("additive placement requires a target group")
        if self.expected_generation is not None and (
            type(self.expected_generation) is not int or self.expected_generation < 0
        ):
            raise ValueError("expected generation must be a non-negative integer")


@dataclass(frozen=True)
class AddTagToConnectionsRequest:
    """Add one tag to multiple connections in one atomic mutation."""

    connection_ids: Tuple[ConnectionId, ...]
    tag: str
    expected_generation: Optional[int] = None

    def __post_init__(self) -> None:
        if type(self.connection_ids) is not tuple or not self.connection_ids:
            raise ValueError("connection ids must be a non-empty tuple")
        for connection_id in self.connection_ids:
            require_identifier(connection_id, "connection id")
        if len(set(self.connection_ids)) != len(self.connection_ids):
            raise ValueError("connection ids must be unique")
        if type(self.tag) is not str or not self.tag.strip():
            raise ValueError("tag must be a non-empty string")
        if "\x00" in self.tag:
            raise ValueError("tag must not contain NUL")
        if self.expected_generation is not None and (
            type(self.expected_generation) is not int or self.expected_generation < 0
        ):
            raise ValueError("expected generation must be a non-negative integer")


@dataclass(frozen=True)
class RenameTagRequest:
    """Rename a tag across all connections (case-insensitive, deduplicated)."""

    old_tag: str
    new_tag: str

    def __post_init__(self) -> None:
        for field_name in ("old_tag", "new_tag"):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a string")
            if "\x00" in value:
                raise ValueError(f"{field_name} must not contain NUL")
        if not self.old_tag.strip():
            raise ValueError("old tag must be a non-empty string")
        if not self.new_tag.strip():
            raise ValueError("new tag must be a non-empty string")
