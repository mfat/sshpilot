"""Compatibility view of daemon-owned connection groups.

Authoritative group names, colors, membership, hierarchy, and ordering
remain snapshot-only.  The daemon client owns all group mutations; this
class only adapts immutable snapshots for older GTK callers.

``ui.group_expansion`` is the sole frontend-owned persistence: exact
Boolean values loaded at construction, persisted by ``set_group_expanded``,
and migrated from legacy ``connection_groups.groups.*.expanded`` once.
"""

from __future__ import annotations

from typing import Optional


class GroupManager:
    """Snapshot-backed compatibility adapter for legacy GTK call sites."""

    def __init__(self, config=None, connection_manager=None, *, client=None, controller=None):
        self.connection_manager = connection_manager
        self.client = client
        self.controller = controller
        self.config = config
        self.groups = {}
        self.connections = {}
        self.root_connections = []
        self._expanded = {}
        self._projection_handler = None
        self._load_expansion_from_config()
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

    def set_mutation_controller(self, controller) -> None:
        self.controller = controller

    def bind_connections(self, _connections) -> None:
        self._refresh()

    def connection_key(self, reference) -> str:
        return str(
            getattr(reference, "id", None)
            or getattr(reference, "nickname", None)
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
        if self.controller is not None:
            return self.controller.create_group(name, parent_id, color or "")
        result = self._require_client().create_group(name, parent_id or "", color or "")
        self._refresh()
        return result

    def delete_group(self, group_id: str):
        if self.controller is not None:
            return self.controller.delete_group(group_id)
        result = self._require_client().delete_group(group_id)
        self._refresh()
        return result

    def rename_group(self, group_id: str, new_name: str):
        if self.controller is not None:
            return self.controller.rename_group(group_id, new_name)
        result = self._require_client().rename_group(group_id, new_name)
        self._refresh()
        return result

    def set_group_color(self, group_id: str, color: Optional[str]):
        from sshpilot.api.models.connection_store import GroupId, SetGroupColorRequest

        if self.controller is not None:
            return self.controller.set_group_color(
                SetGroupColorRequest(group_id=GroupId(group_id), color=color or "")
            )
        result = self._require_client().set_group_color(
            SetGroupColorRequest(group_id=GroupId(group_id), color=color or "")
        )
        self._refresh()
        return result

    def move_connections(self, connection_ids, target_group_id=None,
                         target_connection_id=None, position=None,
                         expected_generation=None, *,
                         on_success=None, on_error=None):
        from sshpilot.api.models.connection_store import MoveConnectionsRequest

        request = MoveConnectionsRequest(
            connection_ids=tuple(self.connection_key(item) for item in connection_ids),
            target_group_id=target_group_id,
            target_connection_id=target_connection_id,
            position=position,
            expected_generation=expected_generation,
        )
        if self.controller is not None:
            return self.controller.run(
                lambda: self._require_client().move_connections(request),
                on_success=on_success or (lambda _result: None),
                on_error=on_error or (lambda _error: None),
            )
        result = self._require_client().move_connections(request)
        self._refresh()
        if on_success is not None:
            on_success(result)
        return result

    def move_connection(self, connection, target_group_id: Optional[str] = None):
        if self.controller is not None:
            return self.controller.move_connection(
                self.connection_key(connection), target_group_id
            )
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

        request = CopyConnectionToGroupRequest(
            connection_id=ConnectionId(self.connection_key(connection)),
            group_id=GroupId(group_id),
        )
        if self.controller is not None:
            return self.controller.copy_connection_to_group(request)
        result = self._require_client().copy_connection_to_group(
            request
        )
        self._refresh()
        return result

    def remove_connection_from_group(self, connection, group_id: str):
        from sshpilot.api.models.connection_store import (
            ConnectionId,
            GroupId,
            RemoveConnectionFromGroupRequest,
        )

        request = RemoveConnectionFromGroupRequest(
            connection_id=ConnectionId(self.connection_key(connection)),
            group_id=GroupId(group_id),
        )
        if self.controller is not None:
            return self.controller.remove_connection_from_group(request)
        result = self._require_client().remove_connection_from_group(request)
        self._refresh()
        return result

    def reorder_connection_in_group(self, connection, target, position="below", group_id=None):
        from sshpilot.api.models.connection_store import (
            ConnectionId,
            GroupId,
            ReorderConnectionRequest,
        )

        request = ReorderConnectionRequest(
            connection_id=ConnectionId(self.connection_key(connection)),
            target_connection_id=ConnectionId(self.connection_key(target)),
            group_id=GroupId(group_id) if group_id else None,
            position=position,
        )
        if self.controller is not None:
            return self.controller.reorder_connection(request)
        result = self._require_client().reorder_connection(request)
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
        def build(group_id, active):
            group = self.groups[group_id]
            if group_id in active:
                raise ValueError("group hierarchy contains a cycle")
            result = dict(group)
            next_active = active | {group_id}
            result["children"] = [
                build(child_id, next_active)
                for child_id in group.get("children", [])
                if child_id in self.groups
            ]
            return result

        return [
            build(group_id, set())
            for group_id, group in self.groups.items()
            if group.get("parent_id") is None
        ]

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

    def sibling_index(self, group_id: str):
        group = self.groups.get(group_id)
        if group is None:
            raise ValueError(f"unknown group: {group_id}")
        parent_id = group.get("parent_id")
        siblings = self.get_ordered_siblings(parent_id)
        for index, sibling in enumerate(siblings):
            if sibling.get("id") == group_id:
                return parent_id, index
        raise ValueError(f"group is missing from sibling projection: {group_id}")

    def set_group_expanded(self, group_id: str, expanded: bool):
        self._expanded[group_id] = bool(expanded)
        if group_id in self.groups:
            self.groups[group_id]["expanded"] = bool(expanded)
        # Persist to ``ui.group_expansion`` so the setting survives restarts.
        if self.config is not None:
            try:
                self.config.set_setting(
                    "ui.group_expansion",
                    dict(self._expanded),
                )
            except Exception:
                pass

    def is_group_expanded(self, group_id: str) -> bool:
        return self._expanded.get(group_id, True)

    def run(self, operation, *, on_success, on_error, refresh_after=True):
        """Delegate to the mutation controller's ``run()``."""
        if self.controller is None:
            raise RuntimeError("no mutation controller attached")
        return self.controller.run(
            operation, on_success=on_success, on_error=on_error,
            refresh_after=refresh_after,
        )

    def run_sequence(self, steps, *, on_success, on_error):
        """Delegate to the mutation controller's ``run_sequence()``."""
        if self.controller is None:
            raise RuntimeError("no mutation controller attached")
        return self.controller.run_sequence(
            steps, on_success=on_success, on_error=on_error,
        )

    def _load_expansion_from_config(self):
        """Load ``ui.group_expansion``; migrate from legacy once."""
        if self.config is None:
            return
        try:
            stored = self.config.get_setting("ui.group_expansion", None)
            if stored is not None and isinstance(stored, dict):
                self._expanded = {
                    str(k): bool(v) for k, v in stored.items()
                }
                return
        except Exception:
            pass
        # One-time migration from legacy ``connection_groups.groups.*.expanded``.
        try:
            legacy = self.config.get_setting("connection_groups", None)
            if legacy is not None and isinstance(legacy, dict):
                groups = legacy.get("groups", {})
                if isinstance(groups, dict):
                    for gid, info in groups.items():
                        if isinstance(info, dict) and "expanded" in info:
                            self._expanded[str(gid)] = bool(info["expanded"])
                    # Write the new overlay so the migration runs only once.
                    if self._expanded:
                        self.config.set_setting(
                            "ui.group_expansion",
                            dict(self._expanded),
                        )
        except Exception:
            pass

    def _load_groups(self):
        self._refresh()

    def _save_groups(self):
        raise RuntimeError("authoritative group persistence belongs to the daemon")
