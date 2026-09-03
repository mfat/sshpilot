"""Best-effort recovery of a v2 sidecar the strict reader rejects.

The strict reader is all-or-nothing by design: one bad entry anywhere makes
the whole document invalid.  That is the right rule for the normal path --
partially-understood state must never be mistaken for the real thing -- but
it is the wrong answer for a file that is already damaged, because the
alternative on offer was refusing every save (see
``docs/architecture/connection-identity-persistence.md``).

Salvage is lenient about the *container* and strict about every *entry*.
Each identity, group, reference and metadata value is rebuilt with the same
constructors and validators the strict reader uses, so nothing here invents,
repairs or reinterprets a value.  An entry that does not validate is dropped
and counted; everything that does validate is kept.

Two kinds of damage are therefore recoverable in full: a document whose shape
drifted (a renamed or missing container, a field of the wrong type) and one
where individual entries are broken while the rest are intact.  A file that
is not JSON at all cannot be salvaged entry-wise and yields an empty result
with everything counted as dropped -- the caller still keeps the original.

Placement is the one thing salvage repairs rather than drops, because
``IdentityStateV2`` requires every active connection to be placed exactly
once.  Dangling and duplicate references are removed, group cycles are
broken, and anything left unplaced is appended to root -- the same repair
``_reconcile_legacy_state`` already performs for v1 documents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .identity_state_v2 import (
    ConnectionReference,
    IdentityStateV2,
    LegacyOrphan,
    PersistedIdentity,
    ReferenceKind,
    UuidGroupState,
    canonical_uuid,
)
from ...api.models.connection_store import validate_safe_metadata

__all__ = ["SalvageReport", "salvage_identity_state_v2"]


@dataclass(frozen=True)
class SalvageReport:
    """What a salvage pass kept and what it could not read.

    ``dropped`` counts are the honest measure of loss and are logged so the
    user is told what a damaged file cost them, rather than silently seeing
    fewer connections than they had.
    """

    identities_kept: int = 0
    identities_dropped: int = 0
    non_ssh_kept: int = 0
    non_ssh_dropped: int = 0
    groups_kept: int = 0
    groups_dropped: int = 0
    metadata_kept: int = 0
    metadata_dropped: int = 0
    placements_repaired: int = 0
    unreadable: bool = False

    @property
    def total_dropped(self) -> int:
        return (
            self.identities_dropped
            + self.non_ssh_dropped
            + self.groups_dropped
            + self.metadata_dropped
        )

    def summary(self) -> str:
        return (
            f"identities={self.identities_kept}/+{self.identities_dropped} "
            f"non_ssh={self.non_ssh_kept}/+{self.non_ssh_dropped} "
            f"groups={self.groups_kept}/+{self.groups_dropped} "
            f"metadata={self.metadata_kept}/+{self.metadata_dropped} "
            f"placements_repaired={self.placements_repaired}"
        )


@dataclass
class _Tally:
    """Mutable counters folded into the frozen report at the end."""

    values: dict = field(default_factory=dict)

    def add(self, key: str, amount: int = 1) -> None:
        self.values[key] = self.values.get(key, 0) + amount


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _salvage_identities(payload: Mapping[str, Any], tally: _Tally):
    """Keep every identity entry that validates strictly, in file order."""
    kept: list[PersistedIdentity] = []
    seen: set[str] = set()
    for identity_uuid, entry in _as_mapping(payload.get("identities")).items():
        try:
            identity = PersistedIdentity.from_dict(identity_uuid, entry)
        except Exception:
            tally.add("identities_dropped")
            continue
        if identity.uuid in seen:
            # A duplicate UUID makes the whole document invalid; the first
            # occurrence is as good a choice as any and keeps file order.
            tally.add("identities_dropped")
            continue
        seen.add(identity.uuid)
        kept.append(identity)
    return tuple(kept)


def _salvage_non_ssh(payload: Mapping[str, Any], tally: _Tally):
    """Non-SSH records live only here, so they are salvaged first-class."""
    kept: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(payload.get("non_ssh_connections")):
        if not isinstance(item, Mapping):
            tally.add("non_ssh_dropped")
            continue
        connection_id = item.get("id") or item.get("nickname")
        if type(connection_id) is not str or not connection_id.strip():
            tally.add("non_ssh_dropped")
            continue
        if connection_id in seen:
            tally.add("non_ssh_dropped")
            continue
        seen.add(connection_id)
        kept.append(dict(item))
    return tuple(kept), seen


def _salvage_groups(payload: Mapping[str, Any], tally: _Tally):
    """Keep valid groups; membership is filtered later against real ids.

    Members are salvaged one at a time rather than through
    ``UuidGroupState.from_dict``, so a single unreadable reference costs that
    reference instead of the whole group and everyone else in it.
    """
    kept: list[UuidGroupState] = []
    seen: set[str] = set()
    for item in _as_list(payload.get("groups")):
        if not isinstance(item, Mapping):
            tally.add("groups_dropped")
            continue
        members: list[ConnectionReference] = []
        for raw_member in _as_list(item.get("members")):
            try:
                members.append(ConnectionReference.from_dict(raw_member))
            except Exception:
                tally.add("placements_repaired")
        try:
            group = UuidGroupState(
                id=item.get("id", ""),
                name=item.get("name", ""),
                members=tuple(members),
                parent_id=item.get("parent_id"),
                order=item.get("order", 0),
                color=item.get("color", ""),
            )
        except Exception:
            tally.add("groups_dropped")
            continue
        if group.id in seen:
            tally.add("groups_dropped")
            continue
        seen.add(group.id)
        kept.append(group)
    return kept


def _break_parent_cycles(groups: list[UuidGroupState]) -> list[UuidGroupState]:
    """Drop a parent link that is unknown or would close a cycle."""
    known = {group.id for group in groups}
    parent_of = {group.id: group.parent_id for group in groups}
    repaired: list[UuidGroupState] = []
    for group in groups:
        parent = group.parent_id
        if parent is not None and parent not in known:
            parent = None
        if parent is not None:
            walker: Optional[str] = parent
            seen = {group.id}
            while walker is not None:
                if walker in seen:
                    parent = None
                    break
                seen.add(walker)
                walker = parent_of.get(walker)
        repaired.append(
            group if parent == group.parent_id
            else UuidGroupState(
                id=group.id,
                name=group.name,
                members=group.members,
                parent_id=parent,
                order=group.order,
                color=group.color,
            )
        )
    return repaired


def _salvage_metadata(
    raw: Any, allowed: set[str], tally: _Tally, *, uuid_keys: bool
):
    """Keep metadata whose key names a surviving record and whose value is safe."""
    kept: dict[str, Mapping[str, Any]] = {}
    for key, values in _as_mapping(raw).items():
        if type(key) is not str or key not in allowed:
            tally.add("metadata_dropped")
            continue
        if uuid_keys:
            try:
                canonical_uuid(key)
            except Exception:
                tally.add("metadata_dropped")
                continue
        if not isinstance(values, Mapping):
            tally.add("metadata_dropped")
            continue
        try:
            validate_safe_metadata(values)
        except Exception:
            tally.add("metadata_dropped")
            continue
        kept[key] = dict(values)
        tally.add("metadata_kept")
    return kept


def _optional_revision(value: Any) -> Optional[str]:
    return value if type(value) is str and value else None


def _place_connections(
    groups: list[UuidGroupState],
    raw_root: list,
    active: list[ConnectionReference],
    tally: _Tally,
):
    """Filter references to real, active records and place every survivor once.

    ``IdentityStateV2`` requires each active connection to appear in exactly
    one group or at root.  Salvage may have removed the record a reference
    pointed at, or kept a document that always violated this, so placement is
    rebuilt rather than trusted.
    """
    valid = set(active)
    placed: set[ConnectionReference] = set()
    repaired_groups: list[UuidGroupState] = []
    for group in groups:
        members: list[ConnectionReference] = []
        for reference in group.members:
            if reference not in valid or reference in placed:
                tally.add("placements_repaired")
                continue
            placed.add(reference)
            members.append(reference)
        repaired_groups.append(
            UuidGroupState(
                id=group.id,
                name=group.name,
                members=tuple(members),
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
            )
        )

    roots: list[ConnectionReference] = []
    for item in raw_root:
        try:
            reference = ConnectionReference.from_dict(item)
        except Exception:
            tally.add("placements_repaired")
            continue
        if reference not in valid or reference in placed:
            tally.add("placements_repaired")
            continue
        placed.add(reference)
        roots.append(reference)

    # Anything that survived salvage but lost its placement still has to be
    # reachable, or the user simply would not see it.
    for reference in active:
        if reference not in placed:
            tally.add("placements_repaired")
            placed.add(reference)
            roots.append(reference)
    return repaired_groups, tuple(roots)


def _salvage_orphans(payload: Mapping[str, Any]):
    kept: list[LegacyOrphan] = []
    for item in _as_list(payload.get("legacy_orphans")):
        try:
            kept.append(LegacyOrphan.from_dict(item))
        except Exception:
            continue
    return tuple(kept)


def salvage_identity_state_v2(raw: bytes) -> tuple[IdentityStateV2, SalvageReport]:
    """Rebuild the most complete valid state the damaged bytes still support.

    Never raises: a caller reaching for salvage has already been refused by
    the strict reader and needs an answer it can proceed with.  The worst
    result is an empty state with ``unreadable`` set.
    """
    tally = _Tally()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return IdentityStateV2(), SalvageReport(unreadable=True)
    if not isinstance(payload, Mapping):
        return IdentityStateV2(), SalvageReport(unreadable=True)

    identities = _salvage_identities(payload, tally)
    non_ssh, non_ssh_ids = _salvage_non_ssh(payload, tally)
    groups = _break_parent_cycles(_salvage_groups(payload, tally))

    active = [
        ConnectionReference(ReferenceKind.SSH_UUID, identity.uuid)
        for identity in identities
        if not identity.tombstone
    ] + [
        ConnectionReference(ReferenceKind.NON_SSH_ID, connection_id)
        for connection_id in non_ssh_ids
    ]
    groups, roots = _place_connections(
        groups, _as_list(payload.get("root_connections")), active, tally
    )

    metadata = _salvage_metadata(
        payload.get("metadata"),
        {identity.uuid for identity in identities},
        tally,
        uuid_keys=True,
    )
    non_ssh_metadata = _salvage_metadata(
        payload.get("non_ssh_metadata"), non_ssh_ids, tally, uuid_keys=False
    )

    generation = payload.get("sidecar_generation")
    if type(generation) is not int or generation < 0:
        generation = 0

    def _report(placement_note: int = 0) -> SalvageReport:
        return SalvageReport(
            identities_kept=len(identities),
            identities_dropped=tally.values.get("identities_dropped", 0),
            non_ssh_kept=len(non_ssh),
            non_ssh_dropped=tally.values.get("non_ssh_dropped", 0),
            groups_kept=len(groups),
            groups_dropped=tally.values.get("groups_dropped", 0),
            metadata_kept=tally.values.get("metadata_kept", 0),
            metadata_dropped=tally.values.get("metadata_dropped", 0),
            placements_repaired=(
                tally.values.get("placements_repaired", 0) + placement_note
            ),
        )

    try:
        state = IdentityStateV2(
            identities=identities,
            groups=tuple(groups),
            root_connections=roots,
            metadata=metadata,
            non_ssh_connections=non_ssh,
            non_ssh_metadata=non_ssh_metadata,
            legacy_orphans=_salvage_orphans(payload),
            # Pending ambiguities are transient diagnostics tied to a specific
            # observed revision.  Their constraints interlock with state this
            # pass may have dropped, and reconciliation recomputes them on the
            # next load, so salvage deliberately starts without any.
            pending_ambiguities=(),
            sidecar_generation=generation,
            last_reconciled_ssh_revision=_optional_revision(
                payload.get("last_reconciled_ssh_revision")
            ),
            observed_ssh_revision=_optional_revision(
                payload.get("observed_ssh_revision")
            ),
        )
        return state, _report()
    except Exception:
        pass

    # An invariant survived every targeted repair above.  Keep the records
    # themselves -- the connections the user would otherwise lose -- and give
    # up the organization around them rather than the whole file.
    try:
        state = IdentityStateV2(
            identities=identities,
            root_connections=tuple(active),
            non_ssh_connections=non_ssh,
            sidecar_generation=generation,
        )
        return state, _report(placement_note=len(groups))
    except Exception:
        return IdentityStateV2(), SalvageReport(
            identities_dropped=len(identities)
            + tally.values.get("identities_dropped", 0),
            non_ssh_dropped=len(non_ssh) + tally.values.get("non_ssh_dropped", 0),
            groups_dropped=len(groups) + tally.values.get("groups_dropped", 0),
            metadata_dropped=tally.values.get("metadata_dropped", 0),
        )
