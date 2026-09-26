"""Effective login profile resolution (pure, GTK-free).

Precedence: a connection's explicit profile, then — for connections linked in
``inherit`` mode — the profile of the nearest ancestor of its primary group.
The primary group is the first group (in store order) that lists the
connection, matching ``GroupManager.get_connection_group``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .models import LINK_EXPLICIT, LINK_INHERIT, LoginProfile, LoginProfileState, ProfileLink

SOURCE_CONNECTION = "connection"
SOURCE_GROUP = "group"


@dataclass(frozen=True)
class EffectiveProfile:
    profile: LoginProfile
    source: str  # SOURCE_CONNECTION | SOURCE_GROUP
    group_id: Optional[str] = None


def primary_group_id(connection_id: str, groups: Sequence) -> Optional[str]:
    for group in groups:
        if connection_id in tuple(getattr(group, "connection_ids", ()) or ()):
            return group.id
    return None


def group_profile(
    group_id: Optional[str],
    groups: Sequence,
    group_links: Mapping[str, str],
    state: LoginProfileState,
) -> Optional[EffectiveProfile]:
    """Nearest profile assigned to *group_id* or one of its ancestors."""
    parents = {g.id: getattr(g, "parent_id", None) for g in groups}
    seen = set()
    current = group_id
    while current and current not in seen:
        seen.add(current)
        profile = state.get(group_links.get(current))
        if profile is not None:
            return EffectiveProfile(profile, SOURCE_GROUP, current)
        current = parents.get(current)
    return None


def effective_profile(
    connection_id: str,
    link: Optional[ProfileLink],
    groups: Sequence,
    group_links: Mapping[str, str],
    state: LoginProfileState,
) -> Optional[EffectiveProfile]:
    if link is None:
        return None
    if link.mode == LINK_EXPLICIT:
        profile = state.get(link.profile_id)
        return EffectiveProfile(profile, SOURCE_CONNECTION) if profile else None
    if link.mode == LINK_INHERIT:
        return group_profile(
            primary_group_id(connection_id, groups), groups, group_links, state
        )
    return None
