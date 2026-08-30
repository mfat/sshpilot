"""Repository adapter between UUID-owned state and the legacy service facade.

This module is deliberately headless.  The loader remains the authority for
SSH projections; this adapter only applies the accepted identity matcher and
projects the resulting UUID state into the temporary alias-shaped
``ConnectionFileState`` consumed by ``ConnectionService``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence, Tuple

from .identity_reconciliation import (
    ConnectionIdentityProjection,
    IdentityRegistryEntry,
    Match,
    MatchReason,
    reconcile_identities,
)
from .identity_state_v2 import (
    ConnectionReference,
    IdentityStateV2,
    PendingAmbiguity,
    PersistedIdentity,
    ReferenceKind,
    UuidGroupState,
    migrate_v1_state,
    new_uuid4,
)
from .models import ConnectionRecord
from .state_file import ConnectionFileState, GroupFileState


def projections_from_records(
    records: Sequence[ConnectionRecord],
) -> Tuple[ConnectionIdentityProjection, ...]:
    """Capture loader evidence before records enter ``ConnectionService``."""

    return tuple(
        ConnectionIdentityProjection.from_record(record, declaration_order=index)
        for index, record in enumerate(records)
        if record.protocol == "ssh"
    )


def _identity_entry_map(state: IdentityStateV2) -> dict[str, PersistedIdentity]:
    return {identity.uuid: identity for identity in state.identities}


def _active_reference(uuid: str) -> ConnectionReference:
    return ConnectionReference(ReferenceKind.SSH_UUID, uuid)


def _placement_values(
    groups_in: Sequence[UuidGroupState],
    root_in: Sequence[ConnectionReference],
    active_uuids: set[str],
    add_root: Sequence[str] = (),
) -> tuple[Tuple[UuidGroupState, ...], Tuple[ConnectionReference, ...]]:
    """Remove retired references while preserving UUID-owned placement."""

    groups = []
    grouped: set[ConnectionReference] = set()
    for group in groups_in:
        members = []
        seen = set()
        for reference in group.members:
            if reference.kind is ReferenceKind.SSH_UUID and reference.value not in active_uuids:
                continue
            if reference in seen:
                continue
            seen.add(reference)
            members.append(reference)
            grouped.add(reference)
        groups.append(replace(group, members=tuple(members)))

    root = []
    seen_root = set()
    for reference in root_in:
        if reference.kind is ReferenceKind.SSH_UUID and reference.value not in active_uuids:
            continue
        if reference in grouped or reference in seen_root:
            continue
        seen_root.add(reference)
        root.append(reference)
    for uuid in add_root:
        reference = _active_reference(uuid)
        if reference not in grouped and reference not in seen_root:
            root.append(reference)
            seen_root.add(reference)
    for uuid in sorted(active_uuids):
        reference = _active_reference(uuid)
        if reference not in grouped and reference not in seen_root:
            root.append(reference)
            seen_root.add(reference)
    return tuple(groups), tuple(root)


def _with_current_placements(
    state: IdentityStateV2,
    *,
    active_uuids: set[str],
    add_root: Sequence[str] = (),
) -> IdentityStateV2:
    groups, root = _placement_values(
        state.groups, state.root_connections, active_uuids, add_root
    )
    return replace(state, groups=groups, root_connections=root)


def migrate_v1_to_identity_state(
    file_state: ConnectionFileState,
    projections: Sequence[ConnectionIdentityProjection],
    *,
    ssh_config_revision: str,
    uuid_factory: Callable[[], str] = new_uuid4,
) -> IdentityStateV2:
    state, _report = migrate_v1_state(
        file_state,
        projections,
        ssh_config_revision=ssh_config_revision,
        uuid_factory=uuid_factory,
    )
    return state


def reconcile_identity_state(
    state: IdentityStateV2,
    projections: Sequence[ConnectionIdentityProjection],
    *,
    ssh_config_revision: str,
    uuid_factory: Callable[[], str] = new_uuid4,
    explicit_continuity: Mapping[str, str] = (),
    allow_tombstone_resurrection: bool = False,
    allow_destination_inference: bool = True,
) -> IdentityStateV2:
    """Apply one frozen reconciliation pass to a persisted v2 snapshot.

    ``explicit_continuity`` maps an old alias to a new alias for an operation
    SSH Pilot itself performed.  Those pairs bypass heuristic evidence while
    all remaining candidates use the accepted matcher unchanged.
    Tombstone resurrection is reserved for explicit SSH authority transitions,
    which are also the transitions that switch destination inference off --
    see ``reconcile_identities``.
    """

    if type(ssh_config_revision) is not str or not ssh_config_revision.strip():
        raise ValueError("SSH config revision must be a non-empty string")
    old_entries = tuple(
        IdentityRegistryEntry(
            uuid=identity.uuid,
            display_name=identity.display_name,
            projection=identity.projection,
            tombstone=identity.tombstone,
            retired_generation=identity.retired_generation,
        )
        for identity in state.identities
    )
    by_old_alias = {
        identity.projection.alias: identity
        for identity in state.identities
        if not identity.tombstone
    }
    by_new_alias = {projection.alias: projection for projection in projections}
    explicit_matches: list[Match] = []
    explicit_old_uuids: set[str] = set()
    explicit_new_aliases: set[str] = set()
    for old_alias, new_alias in dict(explicit_continuity).items():
        identity = by_old_alias.get(old_alias)
        projection = by_new_alias.get(new_alias)
        if identity is None or projection is None:
            raise ValueError("explicit identity continuity does not name current records")
        if identity.uuid in explicit_old_uuids or new_alias in explicit_new_aliases:
            raise ValueError("explicit identity continuity contains duplicates")
        explicit_old_uuids.add(identity.uuid)
        explicit_new_aliases.add(new_alias)
        explicit_matches.append(
            Match(
                IdentityRegistryEntry(
                    uuid=identity.uuid,
                    display_name=identity.display_name,
                    projection=identity.projection,
                ),
                projection,
                MatchReason.EXPLICIT_IN_APP_CONTINUITY,
            )
        )

    remaining_old = tuple(entry for entry in old_entries if entry.uuid not in explicit_old_uuids)
    remaining_new = tuple(
        projection for projection in projections if projection.alias not in explicit_new_aliases
    )

    # An identity still ambiguous coming into this pass was carried forward
    # unchanged and non-tombstoned (see the ``ambiguous_uuids`` branch below),
    # so its frozen, pre-ambiguity projection would otherwise remain live
    # EXACT_ALIAS/anchor evidence on every later pass -- including against a
    # completely unrelated new connection that merely reuses its old, now-
    # free alias. Route each such identity through its own constrained
    # rematch against only its own pending ambiguity's candidate aliases
    # (still using this pass's fresh evidence, so a genuine disambiguating
    # edit -- e.g. one candidate's destination changing -- still resolves it
    # normally); everything else reconciles exactly as before.
    #
    # A pending ambiguity only means something while the aliases it was
    # recorded against are still on offer. Once every candidate alias has left
    # the configuration -- the user switched isolated/default modes back to a
    # config predating the edit that caused the ambiguity, reverted the file,
    # restored a backup -- there is nothing left to resolve it by, and
    # carrying it forward actively corrupts this pass: the identities it names
    # get diverted out of the ordinary pool, find no ``ambiguous_new`` to
    # match against, and are then retired as ghosts against their *own*
    # still-present aliases, losing their UUIDs and display names to freshly
    # created replacements that EXACT_ALIAS should have preserved untouched.
    # Drop the moot ones; the identities they name reconcile normally, which
    # is exactly what a no-longer-contested alias should leave behind.
    remaining_new_aliases = {projection.alias for projection in remaining_new}
    live_ambiguities = tuple(
        ambiguity
        for ambiguity in state.pending_ambiguities
        if any(
            candidate.alias in remaining_new_aliases
            for candidate in ambiguity.new_projections
        )
    )
    still_ambiguous_uuids = {
        uuid for ambiguity in live_ambiguities for uuid in ambiguity.old_uuids
    }
    ambiguity_candidate_aliases = {
        candidate.alias
        for ambiguity in live_ambiguities
        for candidate in ambiguity.new_projections
    }
    ambiguous_old = tuple(
        entry for entry in remaining_old if entry.uuid in still_ambiguous_uuids
    )
    ordinary_old = tuple(
        entry for entry in remaining_old if entry.uuid not in still_ambiguous_uuids
    )
    ambiguous_new = tuple(
        projection for projection in remaining_new
        if projection.alias in ambiguity_candidate_aliases
    )
    ordinary_new = tuple(
        projection for projection in remaining_new
        if projection.alias not in ambiguity_candidate_aliases
    )
    # A ghost's frozen alias can coincide with an unrelated projection that
    # legitimately belongs to the ordinary pool (its own alias was simply
    # reused elsewhere once freed). Two live, non-tombstoned identities must
    # never share one alias, and EXACT_ALIAS is unconditional evidence by
    # design (see MatchReason.EXACT_ALIAS's "cannot be stolen" contract) --
    # so a ghost cannot be given a chance to contest that alias without
    # reintroducing the original bug. Retire it instead: its own frozen
    # alias is conclusively no longer available to reclaim, so nothing sound
    # remains to resolve its ambiguity by.
    #
    # A ghost is retired for a second reason too: renaming one of two colliding
    # aliases away disambiguates nothing. The renamed entry joins the ordinary
    # pool but still sits at the ghost's destination, so it is exactly as
    # likely to be the ghost as the candidate alias that stayed put. Judging
    # the ghost against its recorded candidate aliases alone made that look
    # like a clean one-to-one match and resolved what is really a coin flip,
    # migrating one connection's display name, groups and tags onto the other.
    # It cannot be held back as still-pending either: the state model requires
    # a pending ambiguity's aliases to be unclaimed, and these now belong to
    # ordinary identities. So there is nothing sound left to resolve it by.
    ordinary_new_aliases = {projection.alias for projection in ordinary_new}
    ordinary_new_anchors = {
        projection.destination_anchor
        for projection in ordinary_new
        if projection.destination_anchor is not None
    }
    retiring_ghost_uuids = {
        entry.uuid for entry in ambiguous_old
        if entry.projection.alias in ordinary_new_aliases
        or (
            entry.projection.destination_anchor is not None
            and entry.projection.destination_anchor in ordinary_new_anchors
        )
    }
    if retiring_ghost_uuids:
        ambiguous_old = tuple(
            entry for entry in ambiguous_old if entry.uuid not in retiring_ghost_uuids
        )
    ambiguity_result = reconcile_identities(
        ambiguous_old,
        ambiguous_new,
        uuid_factory=uuid_factory,
        allow_tombstone_resurrection=allow_tombstone_resurrection,
        allow_destination_inference=allow_destination_inference,
    )
    ordinary_result = reconcile_identities(
        ordinary_old,
        ordinary_new,
        uuid_factory=uuid_factory,
        allow_tombstone_resurrection=allow_tombstone_resurrection,
        allow_destination_inference=allow_destination_inference,
    )
    combined_matched = ambiguity_result.matched + ordinary_result.matched
    combined_created = ambiguity_result.created + ordinary_result.created
    combined_deleted = ambiguity_result.deleted + ordinary_result.deleted
    combined_ambiguous = ambiguity_result.ambiguous + ordinary_result.ambiguous

    matches = tuple(explicit_matches) + combined_matched
    deleted_uuids = {entry.uuid for entry in combined_deleted} | retiring_ghost_uuids
    ambiguous_uuids = {
        entry.uuid for item in combined_ambiguous for entry in item.old
    }
    created = {entry.uuid: entry for entry in combined_created}
    next_generation = state.sidecar_generation
    # Where each identity sits right now, so a retirement can remember it and a
    # resurrection can be put back. A tombstone may not be a group member, so
    # this is the only place the folder survives the round trip.
    group_of_uuid = {
        reference.value: group.id
        for group in state.groups
        for reference in group.members
        if reference.kind is ReferenceKind.SSH_UUID
    }
    restored_placements: dict[str, str] = {}
    identities = []
    for identity in state.identities:
        if identity.tombstone:
            # Check if this tombstone should be resurrected (exact alias + anchor match)
            match = next((item for item in matches if item.old.uuid == identity.uuid), None)
            if match is not None:
                # Resurrect: update projection, clear tombstone flag and retired_generation
                if identity.retired_group_id is not None:
                    restored_placements[identity.uuid] = identity.retired_group_id
                identities.append(replace(
                    identity,
                    projection=match.new_projection,
                    tombstone=False,
                    retired_generation=None,
                    retired_group_id=None,
                ))
            else:
                identities.append(identity)
            continue
        match = next((item for item in matches if item.old.uuid == identity.uuid), None)
        if match is not None:
            identities.append(replace(identity, projection=match.new_projection, tombstone=False))
        elif identity.uuid in ambiguous_uuids:
            identities.append(identity)
        elif identity.uuid in deleted_uuids:
            identities.append(
                replace(
                    identity,
                    tombstone=True,
                    retired_generation=next_generation + 1,
                    retired_group_id=group_of_uuid.get(identity.uuid),
                )
            )
        else:
            # A defensive invariant: every old active identity is matched,
            # ambiguous, or deleted by the frozen matcher.
            raise ValueError("reconciliation did not account for an active identity")
    identities.extend(
        PersistedIdentity(
            uuid=entry.uuid,
            display_name=entry.display_name or entry.projection.alias,
            projection=entry.projection,
        )
        for entry in created.values()
    )
    active_uuids = {identity.uuid for identity in identities if not identity.tombstone}
    pending = tuple(
        PendingAmbiguity(
            ssh_config_revision=ssh_config_revision,
            old_uuids=tuple(entry.uuid for entry in item.old),
            new_projections=item.new,
        )
        for item in combined_ambiguous
    )
    placement_groups = state.groups
    if restored_placements:
        placement_groups = tuple(
            replace(
                group,
                members=group.members
                + tuple(
                    _active_reference(uuid)
                    for uuid, group_id in sorted(restored_placements.items())
                    if group_id == group.id
                    and _active_reference(uuid) not in group.members
                ),
            )
            for group in state.groups
        )
    groups, root = _placement_values(
        placement_groups,
        state.root_connections,
        active_uuids,
        tuple(created),
    )
    candidate = replace(
        state,
        identities=tuple(identities),
        groups=groups,
        root_connections=root,
        pending_ambiguities=pending,
        observed_ssh_revision=ssh_config_revision,
        last_reconciled_ssh_revision=(
            state.last_reconciled_ssh_revision if pending else ssh_config_revision
        ),
    )

    # A v1 reference that was quarantined because it was temporarily absent
    # may safely be restored when that exact alias later becomes a newly
    # created SSH identity.  This is placement recovery, not tombstone
    # resurrection: quarantined aliases never had an active UUID.
    created_by_alias = {
        identity.projection.alias: identity.uuid
        for identity in candidate.identities
        if not identity.tombstone
        and identity.uuid in created
    }
    if created_by_alias and candidate.legacy_orphans:
        groups = list(candidate.groups)
        roots = list(candidate.root_connections)
        metadata = dict(candidate.metadata)
        remaining_orphans = []
        changed_by_orphans = False
        for orphan in candidate.legacy_orphans:
            uuid = created_by_alias.get(orphan.alias)
            if uuid is None:
                remaining_orphans.append(orphan)
                continue
            reference = _active_reference(uuid)
            if orphan.kind == "metadata":
                metadata[uuid] = orphan.values
                changed_by_orphans = True
                continue
            if orphan.kind == "group_member":
                group_index = next(
                    (index for index, group in enumerate(groups)
                     if group.id == orphan.group_id),
                    None,
                )
                if group_index is None:
                    remaining_orphans.append(orphan)
                    continue
                members = [item for item in groups[group_index].members if item != reference]
                insert_at = min(orphan.original_index or 0, len(members))
                members.insert(insert_at, reference)
                groups[group_index] = replace(
                    groups[group_index], members=tuple(members)
                )
                roots = [item for item in roots if item != reference]
                changed_by_orphans = True
                continue
            if any(reference in group.members for group in groups):
                remaining_orphans.append(orphan)
                continue
            roots = [item for item in roots if item != reference]
            insert_at = min(orphan.original_index or 0, len(roots))
            roots.insert(insert_at, reference)
            changed_by_orphans = True
        if changed_by_orphans:
            candidate = replace(
                candidate,
                groups=tuple(groups),
                root_connections=tuple(roots),
                metadata=metadata,
                legacy_orphans=tuple(remaining_orphans),
            )
    if candidate == state:
        return state
    return replace(candidate, sidecar_generation=state.sidecar_generation + 1)


def state_to_service_file_state(
    state: IdentityStateV2,
    records: Sequence[ConnectionRecord],
) -> ConnectionFileState:
    """Project UUID-owned placements/metadata to the legacy service shape."""

    alias_by_uuid = {
        identity.uuid: identity.projection.alias
        for identity in state.identities
        if not identity.tombstone
    }
    current_aliases = {record.id for record in records if record.protocol == "ssh"}

    def project(reference: ConnectionReference) -> Optional[str]:
        if reference.kind is ReferenceKind.NON_SSH_ID:
            return reference.value
        alias = alias_by_uuid.get(reference.value)
        return alias if alias in current_aliases else None

    groups = []
    grouped = set()
    for group in state.groups:
        members = []
        for reference in group.members:
            alias = project(reference)
            if alias is not None and alias not in members:
                members.append(alias)
                grouped.add(alias)
        groups.append(
            GroupFileState(
                id=group.id,
                name=group.name,
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
                connection_ids=tuple(members),
            )
        )
    roots = []
    for reference in state.root_connections:
        alias = project(reference)
        if alias is not None and alias not in grouped and alias not in roots:
            roots.append(alias)
    # Ambiguous projections remain launchable in the service facade but have
    # no UUID-owned placement.  Root placement here is runtime-only.
    roots.extend(
        record.id
        for record in records
        if record.protocol == "ssh"
        and record.id not in grouped
        and record.id not in roots
    )
    metadata = {}
    for uuid, values in state.metadata.items():
        alias = alias_by_uuid.get(uuid)
        if alias in current_aliases:
            metadata[alias] = values
    # Non-SSH tags/WoL live in a separate sidecar namespace; fold them into
    # the service metadata view so snapshots and get_metadata() see them.
    for connection_id, values in state.non_ssh_metadata.items():
        metadata[str(connection_id)] = values
    return ConnectionFileState(
        version=1,
        non_ssh_connections=tuple(item for item in state.non_ssh_connections),
        groups=tuple(groups),
        root_connections=tuple(roots),
        metadata=metadata,
    )
