"""Compatibility view of daemon-owned connection groups.

This module deliberately contains no persistence.  The daemon client owns all
group mutations; this class only adapts immutable snapshots for older GTK
callers while group expansion remains frontend-local.
"""

from __future__ import annotations

from typing import Optional


class GroupManager:
    """Snapshot-backed compatibility adapter for legacy GTK call sites."""

    def __init__(self, config=None, connection_manager=None, *, client=None):
        del config
        self.connection_manager = connection_manager
        self.client = client
        self.config = None
        self.groups = {}
        self.connections = {}
        self.root_connections = []
        self._expanded = {}
        self._projection_handler = None
        if connection_manager is not None:
            connect_after = getattr(connection_manager, "connect_after", None)
            if callable(connect_after):
                self._projection_handler = connect_after(
                    "projection-reset", lambda *_args: self._refresh()
                )
        self._refresh()

    def attach_client(self, client) -> None:
        self.client = client
        self._refresh()

    def bind_connections(self, _connections) -> None:
        self._refresh()

    def connection_key(self, reference) -> str:
        return str(
            getattr(reference, "nickname", None)
            or getattr(reference, "id", None)
            or reference
            or ""
        )

    connection_nickname = connection_key

    def _snapshot(self):
        store = self.connection_manager
        if store is None:
            return None
        getter = getattr(store, "snapshot", None)
        return getter() if callable(getter) else None

    def _refresh(self) -> None:
        snapshot = self._snapshot()
        if snapshot is None:
            self.groups = {}
            self.connections = {}
            self.root_connections = []
            return
        grouped = set()
        groups = {}
        for group in snapshot.groups:
            grouped.update(str(cid) for cid in group.connection_ids)
            groups[group.id] = {
                "id": group.id,
                "name": group.name,
                "parent_id": group.parent_id,
                "children": [
                    child.id for child in snapshot.groups if child.parent_id == group.id
                ],
                "connections": list(group.connection_ids),
                "expanded": self._expanded.get(group.id, True),
                "order": group.order,
                "color": group.color,
            }
        self.groups = groups
        self.root_connections = list(snapshot.root_connection_ids)
        self.connections = {
            str(connection.id): self.get_connection_group(connection.id)
            for connection in snapshot.connections
            if str(connection.id) in grouped or connection.id in snapshot.root_connection_ids
        }

    def _require_client(self):
        if self.client is None:
            raise RuntimeError("daemon connection service is unavailable")
        return self.client

    def group_name_exists(self, name: str) -> bool:
        return any(item["name"].lower() == name.lower() for item in self.groups.values())

    def create_group(self, name: str, parent_id: Optional[str] = None, color: Optional[str] = None):
        result = self._require_client().create_group(name, parent_id or "", color or "")
        self._refresh()
        return result

    def delete_group(self, group_id: str):
        result = self._require_client().delete_group(group_id)
        self._refresh()
        return result

    def rename_group(self, group_id: str, new_name: str):
        result = self._require_client().rename_group(group_id, new_name)
        self._refresh()
        return result

    def set_group_color(self, group_id: str, color: Optional[str]):
        from sshpilot.api.models.connection_store import GroupId, SetGroupColorRequest

        result = self._require_client().set_group_color(
            SetGroupColorRequest(group_id=GroupId(group_id), color=color or "")
        )
        self._refresh()
        return result

    def move_connection(self, connection, target_group_id: Optional[str] = None):
        result = self._require_client().assign_connection_to_group(
            self.connection_key(connection), target_group_id or ""
        )
        self._refresh()
        return result

    def copy_connection_to_group(self, connection, group_id: str):
        from sshpilot.api.models.connection_store import (
            ConnectionId,
            CopyConnectionToGroupRequest,
            GroupId,
        )

        result = self._require_client().copy_connection_to_group(
            CopyConnectionToGroupRequest(
                connection_id=ConnectionId(self.connection_key(connection)),
                group_id=GroupId(group_id),
            )
        )
        self._refresh()
        return result

    def remove_connection_from_group(self, connection, group_id: str):
        from sshpilot.api.models.connection_store import (
            ConnectionId,
            GroupId,
            RemoveConnectionFromGroupRequest,
        )

        result = self._require_client().remove_connection_from_group(
            RemoveConnectionFromGroupRequest(
                connection_id=ConnectionId(self.connection_key(connection)),
                group_id=GroupId(group_id),
            )
        )
        self._refresh()
        return result

    def reorder_connection_in_group(self, connection, target, position="below", group_id=None):
        from sshpilot.api.models.connection_store import (
            ConnectionId,
            GroupId,
            ReorderConnectionRequest,
        )

        result = self._require_client().reorder_connection(
            ReorderConnectionRequest(
                connection_id=ConnectionId(self.connection_key(connection)),
                target_connection_id=ConnectionId(self.connection_key(target)),
                group_id=GroupId(group_id) if group_id else None,
                position=position,
            )
        )
        self._refresh()
        return result

    def get_connection_groups(self, connection) -> list[str]:
        key = self.connection_key(connection)
        return [group_id for group_id, group in self.groups.items() if key in group["connections"]]

    def get_connection_group(self, connection) -> Optional[str]:
        groups = self.get_connection_groups(connection)
        return groups[0] if groups else None

    def get_all_groups(self):
        return list(self.groups.values())

    def get_group_hierarchy(self):
        return [group for group in self.groups.values() if group.get("parent_id") is None]

    def resolve_display_group_id(self, connection_reference, context_group_id=None):
        if context_group_id is not None:
            if context_group_id in self.get_connection_groups(connection_reference):
                return context_group_id
        return self.get_connection_group(connection_reference)

    def get_ordered_siblings(self, group_id=None):
        return sorted(
            [g for g in self.groups.values() if g.get("parent_id") == group_id],
            key=lambda g: g.get("order", 0),
        )

    def set_group_expanded(self, group_id: str, expanded: bool):
        self._expanded[group_id] = bool(expanded)
        if group_id in self.groups:
            self.groups[group_id]["expanded"] = bool(expanded)

    def is_group_expanded(self, group_id: str) -> bool:
        return self._expanded.get(group_id, True)

    def _load_groups(self):
        self._refresh()

    def _save_groups(self):
        raise RuntimeError("authoritative group persistence belongs to the daemon")
