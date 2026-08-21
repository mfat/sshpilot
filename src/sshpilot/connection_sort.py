"""Helpers for sorting connections across groups and the root list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Sequence, Tuple

from gettext import gettext as _


@dataclass(frozen=True)
class SortPreset:
    """Describes a connection sorting preset."""

    preset_id: str
    title: str
    description: str
    icon_name: str
    reverse: bool = False

    def __hash__(self) -> int:  # pragma: no cover - required for dataclass
        return hash(self.preset_id)


def _name_key(connection) -> Tuple[str, str, str, str]:
    """Return a tuple used for alphabetical sorting."""
    display_name = str(getattr(connection, "display_name", "") or "")
    nickname = str(getattr(connection, "nickname", "") or "")
    hostname = str(getattr(connection, "hostname", "") or "")
    alias = str(getattr(connection, "host", "") or "")
    primary = display_name or nickname or alias or hostname
    return (primary.casefold(), nickname.casefold(), alias.casefold(), hostname.casefold())


DEFAULT_CONNECTION_SORT = "name-asc"

CONNECTION_SORT_PRESETS: Dict[str, SortPreset] = {
    "name-asc": SortPreset(
        preset_id="name-asc",
        title=_("Name (A-Z)"),
        description=_("Sort connections alphabetically by display name"),
        icon_name="view-sort-ascending-symbolic",
        reverse=False,
    ),
    "name-desc": SortPreset(
        preset_id="name-desc",
        title=_("Name (Z-A)"),
        description=_("Sort connections alphabetically by display name in reverse"),
        icon_name="view-sort-descending-symbolic",
        reverse=True,
    ),
}


def _normalize_key(value: Optional[Sequence[str]]) -> Tuple:
    """Normalize a key so comparisons remain stable."""
    if value is None:
        return ("",)

    if isinstance(value, tuple):
        items = value
    elif isinstance(value, list):
        items = tuple(value)
    else:
        items = (value,)

    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append(item.casefold())
        elif item is None:
            normalized.append("")
        else:
            normalized.append(item)
    return tuple(normalized)


def apply_connection_sort(group_manager, connections: Iterable, preset_id: str) -> bool:
    """
    Reorder connection lists managed by ``group_manager`` using ``preset_id``.

    Returns ``True`` when any ordering changed and persists the new order.
    """

    preset = CONNECTION_SORT_PRESETS.get(preset_id)
    if not preset:
        return False

    lookup = {}
    for connection in connections:
        # Group/root membership is daemon-owned and stores stable connection
        # IDs, while older state and tests may still contain nicknames. Resolve
        # both forms to the same presentation object before deriving the name
        # key; sorting UUID strings directly makes the button appear to do
        # nothing in the current daemon-backed UI.
        for reference in (
            getattr(connection, "id", None),
            getattr(connection, "nickname", None),
        ):
            if reference:
                lookup[str(reference)] = connection

    def _decorated_key(nickname: str) -> Tuple:
        connection = lookup.get(str(nickname))
        if not connection:
            fallback = nickname.casefold()
            return (True, (fallback,), fallback)
        raw_key = _name_key(connection)
        normalized = _normalize_key(raw_key)
        return (False, normalized, nickname.casefold())

    changed = False

    # Sort ungrouped/root connections
    root_connections = getattr(group_manager, "root_connections", [])
    sorted_root = sorted(root_connections, key=_decorated_key, reverse=preset.reverse)
    if list(root_connections) != sorted_root:
        group_manager.root_connections = sorted_root
        changed = True

    # Sort per-group lists
    groups = getattr(group_manager, "groups", {})
    for group in groups.values():
        conn_list = list(group.get("connections", []))
        sorted_list = sorted(conn_list, key=_decorated_key, reverse=preset.reverse)
        if conn_list != sorted_list:
            group["connections"] = sorted_list
            changed = True

    # Sort groups by name (updating their order field)
    # Groups are sorted hierarchically: root groups first, then nested groups
    def _sort_groups_recursive(parent_id=None):
        """Recursively sort groups and update their order field"""
        nonlocal changed
        
        # Get all groups with this parent_id
        groups_with_parent = [
            (group_id, group)
            for group_id, group in groups.items()
            if group.get('parent_id') == parent_id
        ]
        
        if not groups_with_parent:
            return
        
        # Sort groups by name
        def _group_key(item):
            group_id, group = item
            group_name = group.get('name', '')
            return group_name.casefold()
        
        sorted_groups = sorted(groups_with_parent, key=_group_key, reverse=preset.reverse)
        
        # Update order field for each group
        for order, (group_id, group) in enumerate(sorted_groups):
            old_order = group.get('order', 0)
            if old_order != order:
                group['order'] = order
                changed = True
        
        # Recursively sort child groups
        for group_id, group in sorted_groups:
            _sort_groups_recursive(group_id)
    
    # Sort root groups (parent_id is None)
    _sort_groups_recursive(parent_id=None)

    # This ordering is a frontend presentation preference. GroupManager is a
    # daemon-backed projection and deliberately rejects _save_groups(); writing
    # here would both violate ownership and abort the sidebar rebuild after its
    # rows have been cleared. The selected preset is persisted separately by
    # MainWindow and reapplied to each fresh projection.
    return changed
