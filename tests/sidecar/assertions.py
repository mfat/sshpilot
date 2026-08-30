"""Invariant checks shared by the stateful machine and the regression replay.

These functions read the actual persisted ``IdentityStateV2`` sidecar
(production code, not a reimplementation) and compare it against the
harness's own record of what it authored. They never re-derive a
reconciliation decision themselves.
"""

from __future__ import annotations

from typing import Dict, Optional, Set, Tuple

from sshpilot.core.connections.identity_state_v2 import IdentityStateV2

from .model import logical_id_from_marker

AliasState = Dict[str, Tuple[str, str]]  # alias -> (uuid, display_name)


class InvariantViolation(AssertionError):
    pass


class AliasOwnership:
    """The harness's own record of which logical connection authored an alias.

    ``current_owner`` reflects the alias set the harness *just rendered*;
    ``ever_owned`` is append-only so a stale (tombstoned or unresolved
    ambiguous) sidecar entry that still carries a since-renamed alias can be
    checked against who legitimately used to own it.
    """

    def __init__(self) -> None:
        self.current_owner: Dict[str, str] = {}
        self.ever_owned: Dict[str, Set[str]] = {}

    def bind(self, alias: str, logical_id: str) -> None:
        self.current_owner[alias] = logical_id
        self.ever_owned.setdefault(alias, set()).add(logical_id)

    def forget_current(self, alias: str) -> None:
        self.current_owner.pop(alias, None)

    def owner_of(self, alias: str) -> Optional[str]:
        return self.current_owner.get(alias)

    def ever_owned_by(self, alias: str, logical_id: str) -> bool:
        return logical_id in self.ever_owned.get(alias, ())


def check_no_duplicate_active_uuids(state: IdentityStateV2) -> None:
    active = [identity.uuid for identity in state.identities if not identity.tombstone]
    if len(active) != len(set(active)):
        raise InvariantViolation(
            f"duplicate active UUIDs in sidecar identities: {active}"
        )


def check_display_name_ownership(state: IdentityStateV2, ownership: AliasOwnership) -> None:
    """The core "metadata ownership" invariant.

    A ``DISPLAY::<logical_id>`` marker must never be found on an identity
    whose alias the harness never bound to that logical connection -- that
    is the observable signature of metadata silently migrating to an
    unrelated destination.
    """

    for identity in state.identities:
        logical_id = logical_id_from_marker(identity.display_name)
        if logical_id is None:
            continue
        alias = identity.projection.alias
        if not ownership.ever_owned_by(alias, logical_id):
            raise InvariantViolation(
                f"display name marker for logical connection {logical_id!r} is attached to "
                f"alias {alias!r} (uuid={identity.uuid}, tombstone={identity.tombstone}), but "
                f"the harness never bound that alias to {logical_id!r}; it was bound to "
                f"{ownership.ever_owned.get(alias)!r}"
            )
        if identity.tombstone:
            continue
        current_owner = ownership.owner_of(alias)
        if current_owner is not None and current_owner != logical_id:
            raise InvariantViolation(
                f"active identity uuid={identity.uuid} alias={alias!r} carries the display "
                f"name marker for logical connection {logical_id!r}, but the harness currently "
                f"considers alias {alias!r} owned by {current_owner!r}"
            )


def active_alias_state(state: IdentityStateV2) -> AliasState:
    return {
        identity.projection.alias: (identity.uuid, identity.display_name)
        for identity in state.identities
        if not identity.tombstone
    }


def check_exact_alias_continuity(before: AliasState, after: AliasState) -> None:
    """Rule 1 (exact alias) is an unconditional, unconditional-of-everything-else
    production guarantee: see connection-identity-persistence.md's reconciliation
    contract and identity_reconciliation.reconcile_identities's docstring
    ("Exact alias cannot be stolen by a collision group").

    For any alias present as an *active* identity both immediately before and
    after one state transition, with the alias string itself unchanged, its
    UUID and display name must not move -- no matter what else changed
    (HostName, User, Port, IdentityFile, unrelated directives, declaration
    order, source file, restart, mode switch).
    """

    for alias, (uuid, display_name) in after.items():
        previous = before.get(alias)
        if previous is None:
            continue
        before_uuid, before_display = previous
        if before_uuid != uuid:
            raise InvariantViolation(
                f"alias {alias!r} kept its exact name across a mutation but its UUID "
                f"changed from {before_uuid} to {uuid}"
            )
        if before_display != display_name:
            raise InvariantViolation(
                f"alias {alias!r} kept its exact name across a mutation but its display "
                f"name changed from {before_display!r} to {display_name!r}"
            )


def check_state_invariants(state: IdentityStateV2, ownership: AliasOwnership) -> None:
    """Run the cheap, always-applicable checks against one sidecar snapshot.

    Structural integrity (unique group IDs, every reference resolvable, no
    grouped+root double placement, tombstones excluded from placement, ...)
    is already enforced by ``IdentityStateV2.__post_init__`` -- simply having
    constructed ``state`` (e.g. via ``read_identity_state_v2``) means those
    invariants held.
    """

    check_no_duplicate_active_uuids(state)
    check_display_name_ownership(state, ownership)
