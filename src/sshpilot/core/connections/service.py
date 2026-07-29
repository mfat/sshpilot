"""GTK-free connection domain service (canonical reusable state)."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from sshpilot.connection_identity import new_connection_uuid

from ..errors import CoreError, ErrorCode
from ..validation import SSHConnectionValidator
from .models import (
    ConnectionRecord,
    GroupRecord,
    MutationEvent,
    MutationKind,
    generate_duplicate_nickname,
)

Listener = Callable[[MutationEvent], None]


class ConnectionService:
    """In-memory connection/group store with optional JSON persistence.

    GTK adapters may wrap this and emit GObject signals from listeners.
    The service itself never imports GI.
    """

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
        # Snapshot outside lock so listeners can re-enter safely.
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            cb(event)

    def _persist(self) -> None:
        if not self._autosave or self._store_path is None:
            return
        self.save(self._store_path)

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

    def existing_nicknames(self, *, exclude_id: Optional[str] = None) -> set[str]:
        with self._lock:
            return {
                c.nickname
                for c in self._connections.values()
                if exclude_id is None or c.id != exclude_id
            }

    # --- mutations ---------------------------------------------------------

    def create(self, data: Mapping[str, Any]) -> ConnectionRecord:
        payload = dict(data or {})
        # Allow adapters to supply a pre-allocated UUID; otherwise mint one.
        preset_uuid = str(payload.get("uuid") or "").strip() or None
        nickname = str(payload.get("nickname") or payload.get("host") or "").strip()
        hostname = str(payload.get("hostname") or payload.get("host") or "").strip()
        username = str(payload.get("username") or "").strip()
        try:
            port = int(payload.get("port") or 22)
        except (TypeError, ValueError):
            port = 22

        self._validator.set_existing_names({n.lower() for n in self.existing_nicknames()})
        errors: List[str] = []
        if not nickname:
            errors.append("Connection name is required")
        elif nickname.lower() in {n.lower() for n in self.existing_nicknames()}:
            errors.append("Nickname already exists")
        # Nickname whitespace is enforced by the connection dialog UI; plugins
        # (e.g. EasyEnv) historically create spaced Host aliases — domain create
        # must not reject those.
        for result in (
            self._validator.validate_hostname(hostname) if hostname else None,
            self._validator.validate_port(str(port)),
            self._validator.validate_username(username) if username else None,
        ):
            if result is not None and not result.is_valid and result.severity == "error":
                errors.append(result.message)
        if errors:
            raise CoreError(ErrorCode.VALIDATION_ERROR, "; ".join(errors))

        connection_id = preset_uuid or new_connection_uuid()
        if connection_id in self._connections:
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                f"Duplicate connection id {connection_id!r}",
            )
        payload["uuid"] = connection_id
        payload["nickname"] = nickname
        payload.setdefault("hostname", hostname)
        payload.setdefault("username", username)
        payload["port"] = port
        payload.setdefault("protocol", "ssh")

        with self._lock:
            order = len(self._connections)
            group_id = payload.get("group_id")
            if group_id:
                group_id = str(group_id)
                if group_id not in self._groups:
                    # Adapter sync may assign before GroupManager mirrored the group.
                    self._groups[group_id] = GroupRecord(id=group_id, name=group_id)
            record = ConnectionRecord.from_dict(payload, connection_id=connection_id)
            record.order = order
            record.group_id = group_id
            self._connections[connection_id] = record
            if group_id:
                grp = self._groups[group_id]
                if connection_id not in grp.connection_ids:
                    grp.connection_ids.append(connection_id)
            else:
                self._root_order.append(connection_id)
            saved = copy.deepcopy(record)
        self._persist()
        self._emit(MutationEvent(MutationKind.CREATED, connection_id=connection_id))
        return saved

    def upsert(self, data: Mapping[str, Any]) -> ConnectionRecord:
        """Create or update by UUID — used by GTK adapters after SSH-config I/O."""
        payload = dict(data or {})
        connection_id = str(payload.get("uuid") or "").strip()
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
            raise CoreError(ErrorCode.VALIDATION_ERROR, "connection id is required")
        with self._lock:
            existing = self._connections.get(connection_id)
            if existing is None:
                raise CoreError(ErrorCode.VALIDATION_ERROR, f"Unknown connection {connection_id!r}")
            payload = dict(updates or {})
            if "uuid" in payload and str(payload["uuid"]) != connection_id:
                raise CoreError(ErrorCode.VALIDATION_ERROR, "connection UUID is immutable")
            payload.pop("uuid", None)
            if "nickname" in payload:
                nick = str(payload["nickname"]).strip()
                if not nick:
                    raise CoreError(ErrorCode.VALIDATION_ERROR, "Connection name is required")
                lowered = nick.lower()
                for other_id, other in self._connections.items():
                    if other_id != connection_id and other.nickname.lower() == lowered:
                        raise CoreError(ErrorCode.VALIDATION_ERROR, "Nickname already exists")
                # Whitespace allowed — same policy as create() (dialog enforces Host-safe).
                payload = dict(payload)
                payload["nickname"] = nick
            updated = existing.with_updates(payload)
            # Preserve group/order unless explicitly changed
            if "group_id" in payload:
                new_gid = payload.get("group_id")
                new_gid = str(new_gid) if new_gid else None
                if new_gid and new_gid not in self._groups:
                    self._groups[new_gid] = GroupRecord(id=new_gid, name=new_gid)
                old_gid = existing.group_id
                if old_gid and old_gid in self._groups:
                    ids = self._groups[old_gid].connection_ids
                    self._groups[old_gid].connection_ids = [i for i in ids if i != connection_id]
                if existing.id in self._root_order and new_gid:
                    self._root_order = [i for i in self._root_order if i != connection_id]
                if new_gid:
                    grp = self._groups[new_gid]
                    if connection_id not in grp.connection_ids:
                        grp.connection_ids.append(connection_id)
                elif connection_id not in self._root_order:
                    self._root_order.append(connection_id)
                updated.group_id = new_gid
            updated.order = existing.order
            self._connections[connection_id] = updated
            saved = copy.deepcopy(updated)
        self._persist()
        self._emit(MutationEvent(MutationKind.UPDATED, connection_id=connection_id))
        return saved

    def delete(self, connection_id: str) -> None:
        with self._lock:
            existing = self._connections.pop(connection_id, None)
            if existing is None:
                raise CoreError(ErrorCode.VALIDATION_ERROR, f"Unknown connection {connection_id!r}")
            if existing.group_id and existing.group_id in self._groups:
                ids = self._groups[existing.group_id].connection_ids
                self._groups[existing.group_id].connection_ids = [
                    i for i in ids if i != connection_id
                ]
            self._root_order = [i for i in self._root_order if i != connection_id]
        self._persist()
        self._emit(MutationEvent(MutationKind.DELETED, connection_id=connection_id))

    def duplicate(self, connection_id: str) -> ConnectionRecord:
        with self._lock:
            existing = self._connections.get(connection_id)
            if existing is None:
                raise CoreError(ErrorCode.VALIDATION_ERROR, f"Unknown connection {connection_id!r}")
            source = copy.deepcopy(existing)
            nicknames = {c.nickname for c in self._connections.values()}
        new_nick = generate_duplicate_nickname(source.nickname, nicknames)
        payload = source.to_dict()
        payload.pop("uuid", None)
        payload["nickname"] = new_nick
        if source.group_id:
            payload["group_id"] = source.group_id
        created = self.create(payload)
        self._emit(
            MutationEvent(
                MutationKind.DUPLICATED,
                connection_id=created.id,
                detail={"source_id": connection_id},
            )
        )
        return created

    def assign_group(self, connection_id: str, group_id: Optional[str]) -> ConnectionRecord:
        return self.update(connection_id, {"group_id": group_id})

    def remove_from_group(self, connection_id: str) -> ConnectionRecord:
        return self.assign_group(connection_id, None)

    def reorder(self, connection_ids: Sequence[str]) -> None:
        with self._lock:
            seen = set()
            ordered: List[str] = []
            for cid in connection_ids:
                if cid in self._connections and cid not in seen:
                    ordered.append(cid)
                    seen.add(cid)
            missing = [cid for cid in self._connections if cid not in seen]
            ordered.extend(sorted(missing, key=lambda i: self._connections[i].nickname.lower()))
            for index, cid in enumerate(ordered):
                self._connections[cid].order = index
            self._root_order = [
                cid for cid in ordered if self._connections[cid].group_id is None
            ]
        self._persist()
        self._emit(MutationEvent(MutationKind.REORDERED, detail={"order": list(ordered)}))

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
            raise CoreError(ErrorCode.VALIDATION_ERROR, "Group name is required")
        gid = str(group_id or new_connection_uuid()).strip()
        with self._lock:
            if parent_id and parent_id not in self._groups:
                raise CoreError(ErrorCode.VALIDATION_ERROR, f"Unknown parent group {parent_id!r}")
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
            raise CoreError(ErrorCode.VALIDATION_ERROR, "Group id is required")
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

    def delete_group(self, group_id: str, *, move_connections_to_root: bool = True) -> None:
        with self._lock:
            group = self._groups.pop(group_id, None)
            if group is None:
                raise CoreError(ErrorCode.VALIDATION_ERROR, f"Unknown group {group_id!r}")
            for cid in list(group.connection_ids):
                rec = self._connections.get(cid)
                if rec is None:
                    continue
                if move_connections_to_root:
                    rec.group_id = None
                    if cid not in self._root_order:
                        self._root_order.append(cid)
                else:
                    self._connections.pop(cid, None)
            # Detach children groups
            for child in self._groups.values():
                if child.parent_id == group_id:
                    child.parent_id = None
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
        connections = data.get("connections") or []
        if not isinstance(connections, list):
            raise CoreError(ErrorCode.IMPORT_ERROR, "connections must be a list")
        groups_blob = data.get("groups") or {}
        with self._lock:
            if replace:
                self._connections.clear()
                self._groups.clear()
                self._root_order.clear()
            if isinstance(groups_blob, Mapping):
                raw_groups = groups_blob.get("groups") or {}
                if isinstance(raw_groups, Mapping):
                    for gid, gdata in raw_groups.items():
                        if isinstance(gdata, Mapping):
                            payload = dict(gdata)
                            payload.setdefault("id", gid)
                            self._groups[str(payload["id"])] = GroupRecord.from_dict(payload)
            for item in connections:
                if not isinstance(item, Mapping):
                    continue
                rec = ConnectionRecord.from_dict(item)
                if not rec.id:
                    rec.id = new_connection_uuid()
                if not replace and rec.id in self._connections:
                    continue
                if not replace and self.find_by_nickname(rec.nickname):
                    continue
                self._connections[rec.id] = rec
            root = groups_blob.get("root_connections") if isinstance(groups_blob, Mapping) else None
            if isinstance(root, list):
                self._root_order = [str(x) for x in root if str(x) in self._connections]
            else:
                self._root_order = [
                    cid for cid, rec in self._connections.items() if rec.group_id is None
                ]
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
        """Replace domain state from external authoritative sources (e.g. SSH config load)."""
        payload: Dict[str, Any] = {"connections": list(connections), "groups": groups or {}}
        self.load_export_dict(payload, replace=True)

