"""GTK-free connection domain service (canonical reusable state).

In-memory only: ``ConnectionService`` owns no filesystem persistence by
itself (repository persistence lands in the daemon connection repository).
Optional JSON export/import remains for CLI/backup compatibility.

Group membership is derived from ``GroupRecord.connection_ids``; a
connection may belong to any number of groups. ``ConnectionRecord.group_id``
is a compatibility *projection* (the first group containing the connection,
ordered by group order then id) and is never authoritative. The root order
holds only ungrouped connections.

The service never imports GI, never runs subprocesses, and never touches
secret material.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from ..errors import CoreError, ErrorCode
from ..validation import SSHConnectionValidator
from .models import (
    ConnectionRecord,
    GroupRecord,
    MutationEvent,
    MutationKind,
    generate_duplicate_nickname,
    generate_group_slug,
)

Listener = Callable[[MutationEvent], None]


def _validation_error(message: str) -> CoreError:
    return CoreError(ErrorCode.VALIDATION_ERROR, message)


class ConnectionService:
    """In-memory connection/group store with optional JSON export/import."""

    def __init__(
        self,
        *,
        store_path: Optional[os.PathLike] = None,
        autosave: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._connections: Dict[str, ConnectionRecord] = {}
        self._groups: Dict[str, GroupRecord] = {}
        self._root_order: List[str] = []
        self._listeners: List[Listener] = []
        self._store_path = Path(store_path) if store_path else None
        self._autosave = bool(autosave)
        self._validator = SSHConnectionValidator()

    # --- listeners ---------------------------------------------------------

    def add_listener(self, callback: Listener) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Listener) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _emit(self, event: MutationEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            cb(event)

    def _persist(self) -> None:
        if not self._autosave or self._store_path is None:
            return
        self.save(self._store_path)

    # --- membership helpers (groups are the source of truth) ---------------

    def _group_ids_of(self, connection_id: str) -> List[str]:
        """Group ids containing *connection_id*, ordered by (order, id)."""
        return [
            gid
            for gid, g in sorted(
                self._groups.items(), key=lambda kv: (kv[1].order, kv[0])
            )
            if connection_id in g.connection_ids
        ]

    def _sync_connection(self, connection_id: str) -> None:
        """Recompute the ``group_id`` projection and root-order membership."""
        record = self._connections.get(connection_id)
        if record is None:
            return
        groups = self._group_ids_of(connection_id)
        record.group_id = groups[0] if groups else None
        if groups:
            if connection_id in self._root_order:
                self._root_order = [
                    cid for cid in self._root_order if cid != connection_id
                ]
        else:
            if connection_id not in self._root_order:
                self._root_order.append(connection_id)

    def _ensure_group(self, group_id: str, name: str = "") -> GroupRecord:
        """Internal auto-create of a group with a known id (never errors)."""
        existing = self._groups.get(group_id)
        if existing is not None:
            return existing
        record = GroupRecord(id=group_id, name=name or group_id)
        self._groups[group_id] = record
        return record

    def _add_membership(self, connection_id: str, group_id: str) -> None:
        if connection_id not in self._connections:
            raise _validation_error(f"Unknown connection {connection_id!r}")
        group = self._ensure_group(group_id)
        if connection_id not in group.connection_ids:
            group.connection_ids.append(connection_id)
        if connection_id in self._root_order:
            self._root_order = [i for i in self._root_order if i != connection_id]

    def _remove_membership(self, connection_id: str, group_id: str) -> None:
        group = self._groups.get(group_id)
        if group is not None:
            group.connection_ids = [
                cid for cid in group.connection_ids if cid != connection_id
            ]

    def _remove_from_all_groups(self, connection_id: str) -> None:
        for group in self._groups.values():
            group.connection_ids = [
                cid for cid in group.connection_ids if cid != connection_id
            ]

    # --- queries -----------------------------------------------------------

    def list_connections(self) -> List[ConnectionRecord]:
        with self._lock:
            items = list(self._connections.values())
        return sorted(items, key=lambda c: (c.order, c.nickname.lower(), c.id))

    def list_groups(self) -> List[GroupRecord]:
        with self._lock:
            items = list(self._groups.values())
        return sorted(items, key=lambda g: (g.order, g.name.lower(), g.id))

    def get(self, connection_id: str) -> Optional[ConnectionRecord]:
        with self._lock:
            rec = self._connections.get(connection_id)
            return copy.deepcopy(rec) if rec else None

    def find_by_nickname(self, nickname: str) -> Optional[ConnectionRecord]:
        nick = (nickname or "").strip().lower()
        if not nick:
            return None
        with self._lock:
            for rec in self._connections.values():
                if rec.nickname.lower() == nick:
                    return copy.deepcopy(rec)
        return None

    def ordered_records(self) -> List[ConnectionRecord]:
        """Records in daemon load order (insertion order), not sorted."""
        with self._lock:
            return list(self._connections.values())

    def root_order(self) -> List[str]:
        """Ordered ids of ungrouped (root) connections."""
        with self._lock:
            return list(self._root_order)

    def group_ids_of(self, connection_id: str) -> List[str]:
        """Group ids containing *connection_id* in deterministic order."""
        with self._lock:
            return self._group_ids_of(connection_id)

    def existing_nicknames(self, *, exclude_id: Optional[str] = None) -> set[str]:
        with self._lock:
            return {
                c.nickname
                for c in self._connections.values()
                if exclude_id is None or c.id != exclude_id
            }

    # --- connection mutations ----------------------------------------------

    def create(self, data: Mapping[str, Any]) -> ConnectionRecord:
        payload = dict(data or {})
        nickname = str(
            payload.get("nickname")
            or payload.get("id")
            or payload.get("host")
            or ""
        ).strip()
        hostname = str(payload.get("hostname") or payload.get("host") or "").strip()
        username = str(payload.get("username") or "").strip()
        try:
            port = int(payload.get("port") or 22)
        except (TypeError, ValueError):
            port = 22

        self._validator.set_existing_names(
            {n.lower() for n in self.existing_nicknames()}
        )
        errors: List[str] = []
        if not nickname:
            errors.append("Connection name is required")
        elif nickname.lower() in {n.lower() for n in self.existing_nicknames()}:
            errors.append("Nickname already exists")
        for result in (
            self._validator.validate_hostname(hostname) if hostname else None,
            self._validator.validate_port(str(port)),
            self._validator.validate_username(username) if username else None,
        ):
            if result is not None and not result.is_valid and result.severity == "error":
                errors.append(result.message)
        if errors:
            raise _validation_error("; ".join(errors))

        connection_id = nickname
        payload.pop("uuid", None)
        payload["id"] = connection_id
        payload["nickname"] = nickname
        payload.setdefault("hostname", hostname)
        payload.setdefault("username", username)
        payload["port"] = port
        payload.setdefault("protocol", "ssh")

        group_ids = payload.pop("group_ids", None)
        group_id = payload.pop("group_id", None)

        with self._lock:
            order = len(self._connections)
            record = ConnectionRecord.from_dict(payload, connection_id=connection_id)
            record.order = order
            self._connections[connection_id] = record

            requested: List[str] = []
            if isinstance(group_ids, (list, tuple)):
                requested = [str(g) for g in group_ids if str(g)]
            elif group_id:
                requested = [str(group_id)]
            for gid in requested:
                if gid not in self._groups:
                    self._ensure_group(gid)
                self._add_membership(connection_id, gid)
            self._sync_connection(connection_id)
            saved = copy.deepcopy(record)
        self._persist()
        self._emit(MutationEvent(MutationKind.CREATED, connection_id=connection_id))
        return saved

    def upsert(self, data: Mapping[str, Any]) -> ConnectionRecord:
        """Create or update by nickname / alias."""
        payload = dict(data or {})
        connection_id = str(
            payload.get("nickname")
            or payload.get("id")
            or payload.get("host")
            or ""
        ).strip()
        if not connection_id:
            return self.create(payload)
        with self._lock:
            exists = connection_id in self._connections
        if exists:
            return self.update(connection_id, payload)
        return self.create(payload)

    def delete_if_present(self, connection_id: str) -> bool:
        with self._lock:
            if connection_id not in self._connections:
                return False
        self.delete(connection_id)
        return True

    def update(self, connection_id: str, updates: Mapping[str, Any]) -> ConnectionRecord:
        if not connection_id:
            raise _validation_error("connection id is required")
        with self._lock:
            existing = self._connections.get(connection_id)
            if existing is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            payload = dict(updates or {})
            payload.pop("uuid", None)
            new_nick = None
            if "nickname" in payload:
                nick = str(payload["nickname"]).strip()
                if not nick:
                    raise _validation_error("Connection name is required")
                lowered = nick.lower()
                for other_id, other in self._connections.items():
                    if other_id != connection_id and other.nickname.lower() == lowered:
                        raise _validation_error("Nickname already exists")
                payload = dict(payload)
                payload["nickname"] = nick
                if nick != connection_id:
                    new_nick = nick

            target_id = new_nick if new_nick else connection_id
            updated = existing.with_updates(payload)
            updated.id = target_id
            updated.nickname = target_id

            if new_nick:
                self._connections.pop(connection_id, None)
                # Rename every reference in every group and the root order.
                for group in self._groups.values():
                    group.connection_ids = [
                        target_id if i == connection_id else i
                        for i in group.connection_ids
                    ]
                self._root_order = [
                    target_id if i == connection_id else i for i in self._root_order
                ]
                existing.group_id = None  # projection recomputed below

            if "group_id" in payload or "group_ids" in payload:
                new_gid = payload.get("group_id")
                new_gids: List[str] = []
                raw_multi = payload.get("group_ids")
                if isinstance(raw_multi, (list, tuple)):
                    new_gids = [str(g) for g in raw_multi if str(g)]
                elif new_gid:
                    new_gids = [str(new_gid)]
                self._remove_from_all_groups(connection_id)
                for gid in new_gids:
                    if gid not in self._groups:
                        self._ensure_group(gid)
                    self._add_membership(connection_id, gid)

            updated.order = existing.order
            self._connections[target_id] = updated
            self._sync_connection(target_id)
            saved = copy.deepcopy(updated)

        self._persist()
        if new_nick:
            self._emit(MutationEvent(MutationKind.DELETED, connection_id=connection_id))
            self._emit(MutationEvent(MutationKind.CREATED, connection_id=target_id))
        else:
            self._emit(MutationEvent(MutationKind.UPDATED, connection_id=connection_id))
        return saved

    def delete(self, connection_id: str) -> None:
        with self._lock:
            existing = self._connections.pop(connection_id, None)
            if existing is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            self._remove_from_all_groups(connection_id)
            self._root_order = [i for i in self._root_order if i != connection_id]
        self._persist()
        self._emit(MutationEvent(MutationKind.DELETED, connection_id=connection_id))

    def duplicate(self, connection_id: str) -> ConnectionRecord:
        with self._lock:
            existing = self._connections.get(connection_id)
            if existing is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            source = copy.deepcopy(existing)
            nicknames = {c.nickname for c in self._connections.values()}
        new_nick = generate_duplicate_nickname(source.nickname, nicknames)
        payload = source.to_dict()
        payload.pop("uuid", None)
        payload["id"] = new_nick
        payload["nickname"] = new_nick
        payload["group_ids"] = self._group_ids_of(connection_id)
        created = self.create(payload)
        self._emit(
            MutationEvent(
                MutationKind.DUPLICATED,
                connection_id=created.id,
                detail={"source_id": connection_id},
            )
        )
        return created

    # --- membership mutations ----------------------------------------------

    def assign_group(
        self, connection_id: str, group_id: Optional[str]
    ) -> ConnectionRecord:
        """Move *connection_id* into exactly one group (removes all others)."""
        with self._lock:
            if self._connections.get(connection_id) is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            self._remove_from_all_groups(connection_id)
            if group_id:
                if group_id not in self._groups:
                    self._ensure_group(group_id)
                self._add_membership(connection_id, str(group_id))
            self._sync_connection(connection_id)
            saved = copy.deepcopy(self._connections[connection_id])
        self._persist()
        self._emit(
            MutationEvent(
                MutationKind.GROUP_ASSIGNED,
                connection_id=connection_id,
                group_id=group_id,
            )
        )
        return saved

    def remove_from_group(self, connection_id: str) -> ConnectionRecord:
        """Remove *connection_id* from every group (moves to root)."""
        return self.assign_group(connection_id, None)

    def copy_connection_to_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        """Add one membership without removing existing ones."""
        with self._lock:
            if self._connections.get(connection_id) is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            self._add_membership(connection_id, str(group_id))
            self._sync_connection(connection_id)
            saved = copy.deepcopy(self._connections[connection_id])
        self._persist()
        self._emit(
            MutationEvent(
                MutationKind.GROUP_ASSIGNED,
                connection_id=connection_id,
                group_id=str(group_id),
            )
        )
        return saved

    def remove_connection_from_group(
        self, connection_id: str, group_id: str
    ) -> ConnectionRecord:
        """Remove exactly one membership."""
        with self._lock:
            if self._connections.get(connection_id) is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            self._remove_membership(connection_id, str(group_id))
            self._sync_connection(connection_id)
            saved = copy.deepcopy(self._connections[connection_id])
        self._persist()
        self._emit(
            MutationEvent(
                MutationKind.GROUP_REMOVED,
                connection_id=connection_id,
                group_id=str(group_id),
            )
        )
        return saved

    def reorder(self, connection_ids: Sequence[str]) -> None:
        """Replace the global ordering; root order keeps only ungrouped ones."""
        with self._lock:
            seen = set()
            ordered: List[str] = []
            for cid in connection_ids:
                if cid in self._connections and cid not in seen:
                    ordered.append(cid)
                    seen.add(cid)
            missing = [cid for cid in self._connections if cid not in seen]
            ordered.extend(
                sorted(missing, key=lambda i: self._connections[i].nickname.lower())
            )
            for index, cid in enumerate(ordered):
                self._connections[cid].order = index
            self._root_order = [
                cid for cid in ordered if not self._group_ids_of(cid)
            ]
        self._persist()
        self._emit(MutationEvent(MutationKind.REORDERED, detail={"order": list(ordered)}))

    def move_connections(self, request) -> None:
        """Place a connection block using explicit membership semantics."""
        connection_ids = tuple(str(item) for item in request.connection_ids)
        target_group_id = (
            str(request.target_group_id) if request.target_group_id is not None else None
        )
        target_id = (
            str(request.target_connection_id)
            if request.target_connection_id is not None
            else None
        )
        position = request.position
        raw_mode = getattr(request, "mode", "exclusive")
        mode = getattr(raw_mode, "value", raw_mode)
        raw_source_group_id = getattr(request, "source_group_id", None)
        source_group_id = (
            str(raw_source_group_id) if raw_source_group_id is not None else None
        )
        with self._lock:
            for connection_id in connection_ids:
                if connection_id not in self._connections:
                    raise _validation_error(f"Unknown connection {connection_id!r}")
            if target_group_id is not None and target_group_id not in self._groups:
                raise _validation_error(f"Unknown group {target_group_id!r}")
            if source_group_id is not None and source_group_id not in self._groups:
                raise _validation_error(f"Unknown group {source_group_id!r}")
            if source_group_id is not None:
                source_members = self._groups[source_group_id].connection_ids
                if any(connection_id not in source_members for connection_id in connection_ids):
                    raise _validation_error("source group does not contain every connection")
            elif mode == "preserve" and any(
                connection_id not in self._root_order for connection_id in connection_ids
            ):
                raise _validation_error("root source does not contain every connection")
            if target_id is not None and target_id not in self._connections:
                raise _validation_error(f"Unknown connection {target_id!r}")
            container = (
                self._groups[target_group_id].connection_ids
                if target_group_id is not None
                else self._root_order
            )
            if target_id is not None and target_id not in container:
                raise _validation_error("target connection is outside the destination")
            if mode == "additive":
                for connection_id in connection_ids:
                    self._add_membership(connection_id, target_group_id)
                    self._sync_connection(connection_id)
            else:
                if mode == "exclusive":
                    for connection_id in connection_ids:
                        self._remove_from_all_groups(connection_id)
                        self._root_order = [
                            item for item in self._root_order if item != connection_id
                        ]
                else:
                    container[:] = [
                        item for item in container if item not in connection_ids
                    ]
                container = (
                    self._groups[target_group_id].connection_ids
                    if target_group_id is not None
                    else self._root_order
                )
                insert_at = len(container)
                if target_id is not None:
                    insert_at = container.index(target_id)
                    if position == "below":
                        insert_at += 1
                container[insert_at:insert_at] = list(connection_ids)
                for connection_id in connection_ids:
                    if mode == "preserve" and target_group_id is not None:
                        self._add_membership(connection_id, target_group_id)
                    self._sync_connection(connection_id)
        self._persist()
        self._emit(
            MutationEvent(
                MutationKind.REORDERED,
                connection_id=connection_ids[0],
                group_id=target_group_id,
                detail={
                    "connection_ids": list(connection_ids),
                    "position": position,
                    "mode": mode,
                },
            )
        )

    def reorder_connection(
        self,
        connection_id: str,
        target_connection_id: str,
        group_id: Optional[str],
        position: str,
    ) -> None:
        """Place *connection_id* above/below *target_connection_id*.

        Ordering applies within a group when *group_id* is given, otherwise
        within the root list. ``position`` is exactly ``"above"`` or
        ``"below"``.
        """
        if position not in ("above", "below"):
            raise _validation_error("position must be 'above' or 'below'")
        with self._lock:
            if self._connections.get(connection_id) is None:
                raise _validation_error(f"Unknown connection {connection_id!r}")
            if group_id:
                if group_id not in self._groups:
                    raise _validation_error(f"Unknown group {group_id!r}")
                if connection_id not in self._groups[group_id].connection_ids:
                    # The connection is being placed into this group.
                    self._add_membership(connection_id, group_id)
                container = self._groups[group_id].connection_ids
            else:
                if connection_id not in self._root_order:
                    self._remove_from_all_groups(connection_id)
                    self._root_order.append(connection_id)
                container = self._root_order

            container = [cid for cid in container if cid != connection_id]
            try:
                target_index = container.index(target_connection_id)
            except ValueError:
                target_index = len(container)
            insert_at = target_index if position == "above" else target_index + 1
            container.insert(insert_at, connection_id)

            if group_id:
                self._groups[group_id].connection_ids = container
            else:
                self._root_order = container
            self._sync_connection(connection_id)
        self._persist()
        self._emit(
            MutationEvent(
                MutationKind.REORDERED,
                connection_id=connection_id,
                group_id=group_id,
                detail={"position": position},
            )
        )

    # --- groups ------------------------------------------------------------

    def create_group(
        self,
        name: str,
        *,
        parent_id: Optional[str] = None,
        group_id: Optional[str] = None,
        color: str = "",
        order: Optional[int] = None,
    ) -> GroupRecord:
        name = (name or "").strip()
        if not name:
            raise _validation_error("Group name is required")
        with self._lock:
            if parent_id and parent_id not in self._groups:
                raise _validation_error(f"Unknown parent group {parent_id!r}")
            gid = str(
                group_id or generate_group_slug(name, set(self._groups.keys()))
            ).strip()
            if gid in self._groups:
                existing = self._groups[gid]
                existing.name = name
                existing.parent_id = parent_id
                if color:
                    existing.color = color
                saved = copy.deepcopy(existing)
                self._persist()
                return saved
            record = GroupRecord(
                id=gid,
                name=name,
                parent_id=parent_id,
                order=len(self._groups) if order is None else int(order),
                color=color or "",
            )
            self._groups[gid] = record
            saved = copy.deepcopy(record)
        self._persist()
        self._emit(MutationEvent(MutationKind.GROUP_CREATED, group_id=gid))
        return saved

    def ensure_group(
        self,
        group_id: str,
        *,
        name: str = "",
        parent_id: Optional[str] = None,
        color: str = "",
        order: Optional[int] = None,
    ) -> GroupRecord:
        """Create a group with a known id if missing (GTK GroupManager sync)."""
        gid = str(group_id or "").strip()
        if not gid:
            raise _validation_error("Group id is required")
        if parent_id:
            parent_id = str(parent_id)
            with self._lock:
                missing_parent = parent_id not in self._groups
            if missing_parent:
                self.ensure_group(parent_id, name=parent_id)
        with self._lock:
            existing = self._groups.get(gid)
            if existing is not None:
                return copy.deepcopy(existing)
        return self.create_group(
            name or gid,
            parent_id=parent_id,
            group_id=gid,
            color=color,
            order=order,
        )

    def _would_create_cycle(self, group_id: str, parent_id: Optional[str]) -> bool:
        if parent_id is None or parent_id == group_id:
            return parent_id == group_id
        seen = {group_id}
        current = parent_id
        while current is not None:
            if current in seen:
                return True
            seen.add(current)
            group = self._groups.get(current)
            current = group.parent_id if group is not None else None
        return False

    def place_group(
        self,
        group_id: str,
        parent_id: Optional[str],
        index: int,
    ) -> GroupRecord:
        """Move *group_id* beneath *parent_id* at sibling *index*.

        Rejects parent cycles (a group may never be its own ancestor).
        """
        if type(index) is not int or index < 0:
            raise _validation_error("index must be a non-negative integer")
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise _validation_error(f"Unknown group {group_id!r}")
            if parent_id and parent_id not in self._groups:
                raise _validation_error(f"Unknown parent group {parent_id!r}")
            if self._would_create_cycle(group_id, parent_id):
                raise _validation_error("Group placement would create a parent cycle")
            group.parent_id = parent_id
            siblings = [
                g
                for g in self._groups.values()
                if g.id != group_id and g.parent_id == parent_id
            ]
            siblings.sort(key=lambda g: (g.order, g.id))
            siblings.insert(min(index, len(siblings)), group)
            for i, sibling in enumerate(siblings):
                sibling.order = i
            saved = copy.deepcopy(group)
        self._persist()
        self._emit(MutationEvent(MutationKind.GROUP_CREATED, group_id=group_id))
        return saved

    def rename_group(self, group_id: str, new_name: str) -> GroupRecord:
        """Rename a group (the id is stable; memberships are unaffected)."""
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise _validation_error(f"Unknown group {group_id!r}")
            name = (new_name or "").strip()
            if not name:
                raise _validation_error("Group name is required")
            group.name = name
            saved = copy.deepcopy(group)
        self._persist()
        self._emit(MutationEvent(MutationKind.GROUP_CREATED, group_id=group_id))
        return saved

    def set_group_color(self, group_id: str, color: str) -> GroupRecord:
        """Update a group's color (empty string clears it)."""
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise _validation_error(f"Unknown group {group_id!r}")
            group.color = str(color or "")
            saved = copy.deepcopy(group)
        self._persist()
        return saved

    def delete_group(self, group_id: str, *, move_connections_to_root: bool = True) -> None:
        """Delete *group_id*.

        Children move to the deleted group's parent; connections keep every
        other membership and move to root only when this was their last group.
        """
        with self._lock:
            group = self._groups.pop(group_id, None)
            if group is None:
                raise _validation_error(f"Unknown group {group_id!r}")
            # Detach the group's own connections (preserving other memberships).
            for cid in list(group.connection_ids):
                if cid in self._connections and not move_connections_to_root:
                    self._connections.pop(cid, None)
                    self._remove_from_all_groups(cid)
                    self._root_order = [i for i in self._root_order if i != cid]
            # Children move to the deleted group's parent.
            for child in self._groups.values():
                if child.parent_id == group_id:
                    child.parent_id = group.parent_id
            if move_connections_to_root:
                for cid in list(group.connection_ids):
                    if cid in self._connections:
                        self._sync_connection(cid)
        self._persist()
        self._emit(MutationEvent(MutationKind.GROUP_DELETED, group_id=group_id))

    # --- persistence / import-export ---------------------------------------

    def to_export_dict(self) -> Dict[str, object]:
        with self._lock:
            return {
                "version": 1,
                "connections": [c.to_dict() for c in self.list_connections()],
                "groups": {
                    "groups": {gid: g.to_dict() for gid, g in self._groups.items()},
                    "connections": {
                        cid: rec.group_id
                        for cid, rec in self._connections.items()
                        if rec.group_id
                    },
                    "root_connections": list(self._root_order),
                },
            }

    def load_export_dict(self, data: Mapping[str, Any], *, replace: bool = True) -> None:
        if not isinstance(data, Mapping):
            raise CoreError(ErrorCode.IMPORT_ERROR, "Export payload must be a mapping")
        connections_raw = data.get("connections") or []
        if not isinstance(connections_raw, list):
            raise CoreError(ErrorCode.IMPORT_ERROR, "connections must be a list")
        groups_blob = data.get("groups") or {}

        # Build a candidate state first; publish only when every step succeeds.
        connections: Dict[str, ConnectionRecord] = {}
        groups: Dict[str, GroupRecord] = {}
        if isinstance(groups_blob, Mapping):
            raw_groups = groups_blob.get("groups") or {}
            if isinstance(raw_groups, Mapping):
                for gid, gdata in raw_groups.items():
                    if isinstance(gdata, Mapping):
                        payload = dict(gdata)
                        payload.setdefault("id", gid)
                        groups[str(payload["id"])] = GroupRecord.from_dict(payload)

        with self._lock:
            if not replace:
                connections = dict(self._connections)
                groups = dict(self._groups)
            for item in connections_raw:
                if not isinstance(item, Mapping):
                    continue
                rec = ConnectionRecord.from_dict(item)
                if not rec.id:
                    continue
                if not replace and rec.id in connections:
                    continue
                if not replace and self.find_by_nickname(rec.nickname):
                    continue
                connections[rec.id] = rec

            # Reconcile membership from every legacy encoding.
            for cid, rec in connections.items():
                requested: List[str] = []
                multi = rec.data.get("group_ids") if isinstance(rec.data, dict) else None
                if isinstance(multi, (list, tuple)):
                    requested = [str(g) for g in multi if str(g)]
                elif rec.data.get("group_id"):
                    requested = [str(rec.data["group_id"])]
                if requested:
                    for gid in requested:
                        group = groups.get(gid)
                        if group is None:
                            group = GroupRecord(id=gid, name=gid)
                            groups[gid] = group
                        if cid not in group.connection_ids:
                            group.connection_ids.append(cid)
            if isinstance(groups_blob, Mapping):
                legacy_map = groups_blob.get("connections") or {}
                if isinstance(legacy_map, Mapping):
                    for cid, gid in legacy_map.items():
                        cid, gid = str(cid), str(gid)
                        if cid not in connections:
                            continue
                        group = groups.get(gid)
                        if group is None:
                            group = GroupRecord(id=gid, name=gid)
                            groups[gid] = group
                        if cid not in group.connection_ids:
                            group.connection_ids.append(cid)

            root_raw = (
                groups_blob.get("root_connections")
                if isinstance(groups_blob, Mapping)
                else None
            )
            root_order: List[str] = []
            if isinstance(root_raw, list):
                root_order = [str(x) for x in root_raw if str(x) in connections]

            self._connections = connections
            self._groups = groups
            self._root_order = root_order
            for cid in list(self._connections):
                self._sync_connection(cid)
        self._persist()

    def save(self, path: os.PathLike) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_export_dict(), indent=2, sort_keys=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)

    def load(self, path: os.PathLike) -> None:
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.load_export_dict(data, replace=True)

    # --- sync helpers for GTK adapter --------------------------------------

    def replace_all(
        self,
        connections: Iterable[Mapping[str, Any]],
        *,
        groups: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Replace domain state from external authoritative sources.

        Transactional: a candidate state is validated and only published when
        every record and group parses successfully.
        """
        payload: Dict[str, Any] = {
            "connections": list(connections),
            "groups": groups or {},
        }
        self.load_export_dict(payload, replace=True)
