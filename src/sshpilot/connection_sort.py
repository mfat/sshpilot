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
    #: ``True`` for the pass-through preset that shows the daemon's own order.
    manual: bool = False

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


#: Pass-through preset: show the ordering the daemon reports, untouched.
MANUAL_CONNECTION_SORT = "manual"

#: A list nobody has arranged yet reads best alphabetically.
DEFAULT_CONNECTION_SORT = "name-asc"

#: Config key holding the selected preset id.
CONNECTION_SORT_SETTING = "ui.connection_sort"

CONNECTION_SORT_PRESETS: Dict[str, SortPreset] = {
    MANUAL_CONNECTION_SORT: SortPreset(
        preset_id=MANUAL_CONNECTION_SORT,
        title=_("Manual order"),
        description=_("Show connections in the order you arranged them"),
        icon_name="view-list-symbolic",
        reverse=False,
        manual=True,
    ),
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

#: Choices in the sort button's menu, top to bottom.
CONNECTION_SORT_MENU = ("name-asc", "name-desc", MANUAL_CONNECTION_SORT)


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
    Reorder the frontend group projection using ``preset_id``.

    Returns ``True`` when any ordering changed.
    """

    preset = CONNECTION_SORT_PRESETS.get(preset_id)
    if not preset or preset.manual:
        # Manual order *is* the daemon projection: there is nothing to reorder,
        # and the caller restores it by refreshing the projection instead.
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

    # GroupManager is daemon-backed. Keep sorting presentation-only here;
    # ordinary rebuilds must preserve the authoritative daemon ordering so
    # manual drag-and-drop reordering is not overwritten.
    return changed


def load_connection_sort(config) -> str:
    """The saved preset id, or the default when none (or an unknown one) is saved."""
    try:
        value = config.get_setting(CONNECTION_SORT_SETTING, None)
    except Exception:
        value = None
    return value if value in CONNECTION_SORT_PRESETS else DEFAULT_CONNECTION_SORT


def _ordered_groups(groups):
    """Groups in sibling order, ties kept in daemon order like the sidebar.

    Only the order among groups sharing a parent matters to the layout.
    """
    return sorted(groups, key=lambda group: group[1])


def layout_request_from_projection(group_manager, expected_generation=None):
    """The arrangement ``group_manager`` shows, as a layout request."""
    from .api.models.connection_store import GroupLayout, SetConnectionLayoutRequest

    groups = getattr(group_manager, "groups", {}) or {}
    entries = _ordered_groups(
        (str(group_id), int(info.get("order", 0) or 0), info)
        for group_id, info in groups.items()
    )
    return SetConnectionLayoutRequest(
        root_connection_ids=tuple(
            str(cid) for cid in getattr(group_manager, "root_connections", [])
        ),
        groups=tuple(
            GroupLayout(
                group_id=group_id,
                parent_id=info.get("parent_id") or None,
                connection_ids=tuple(str(cid) for cid in info.get("connections", [])),
            )
            for group_id, _order, info in entries
        ),
        expected_generation=expected_generation,
    )


def layout_request_from_snapshot(snapshot):
    """The daemon's stored arrangement, as a layout request (for Undo)."""
    from .api.models.connection_store import GroupLayout, SetConnectionLayoutRequest

    entries = _ordered_groups(
        (str(group.id), int(group.order), group) for group in snapshot.groups
    )
    return SetConnectionLayoutRequest(
        root_connection_ids=tuple(str(cid) for cid in snapshot.root_connection_ids),
        groups=tuple(
            GroupLayout(
                group_id=group_id,
                parent_id=group.parent_id or None,
                connection_ids=tuple(str(cid) for cid in group.connection_ids),
            )
            for group_id, _order, group in entries
        ),
    )
