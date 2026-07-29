"""GTK-free connection and group domain models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional
import copy


class MutationKind(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    DUPLICATED = "duplicated"
    REORDERED = "reordered"
    GROUP_ASSIGNED = "group_assigned"
    GROUP_REMOVED = "group_removed"
    GROUP_CREATED = "group_created"
    GROUP_DELETED = "group_deleted"


@dataclass(frozen=True)
class MutationEvent:
    """Domain change notification — no GObject / GTK."""

    kind: MutationKind
    connection_id: Optional[str] = None
    group_id: Optional[str] = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionRecord:
    """Canonical reusable connection state (plain data)."""

    id: str
    nickname: str
    hostname: str = ""
    username: str = ""
    port: int = 22
    protocol: str = "ssh"
    group_id: Optional[str] = None
    order: int = 0
    data: Dict[str, Any] = field(default_factory=dict)

    def normalized_nickname(self) -> str:
        return (self.nickname or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        payload = copy.deepcopy(self.data) if self.data else {}
        payload.update(
            {
                "uuid": self.id,
                "nickname": self.nickname,
                "hostname": self.hostname,
                "username": self.username,
                "port": int(self.port),
                "protocol": self.protocol or "ssh",
            }
        )
        if self.group_id is not None:
            payload["group_id"] = self.group_id
        payload["order"] = int(self.order)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, connection_id: Optional[str] = None) -> "ConnectionRecord":
        raw = dict(data or {})
        cid = connection_id or str(raw.get("uuid") or "").strip()
        nick = str(raw.get("nickname") or raw.get("host") or "").strip()
        try:
            port = int(raw.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        return cls(
            id=cid,
            nickname=nick,
            hostname=str(raw.get("hostname") or raw.get("host") or "").strip(),
            username=str(raw.get("username") or "").strip(),
            port=port,
            protocol=str(raw.get("protocol") or "ssh").strip() or "ssh",
            group_id=(str(raw["group_id"]) if raw.get("group_id") else None),
            order=int(raw.get("order") or 0),
            data=raw,
        )

    def with_updates(self, updates: Mapping[str, Any]) -> "ConnectionRecord":
        merged = copy.deepcopy(self.data)
        merged.update(dict(updates))
        merged["uuid"] = self.id  # UUID is immutable
        if "nickname" in updates:
            merged["nickname"] = str(updates["nickname"]).strip()
        return ConnectionRecord.from_dict(merged, connection_id=self.id)


@dataclass
class GroupRecord:
    """Hierarchical connection group."""

    id: str
    name: str
    parent_id: Optional[str] = None
    order: int = 0
    connection_ids: List[str] = field(default_factory=list)
    collapsed: bool = False
    color: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "order": int(self.order),
            "connections": list(self.connection_ids),
            "collapsed": bool(self.collapsed),
            "color": self.color or "",
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupRecord":
        raw = dict(data or {})
        gid = str(raw.get("id") or "").strip()
        conns = raw.get("connections") or raw.get("connection_ids") or []
        return cls(
            id=gid,
            name=str(raw.get("name") or "").strip() or gid,
            parent_id=(str(raw["parent_id"]) if raw.get("parent_id") else None),
            order=int(raw.get("order") or 0),
            connection_ids=[str(c) for c in conns if c],
            collapsed=bool(raw.get("collapsed")),
            color=str(raw.get("color") or ""),
        )


def generate_duplicate_nickname(base_nickname: str, existing: set[str], *, copy_token: str = "Copy") -> str:
    """Whitespace-free duplicate nickname (ssh Host alias safe)."""
    existing_lower = {n.lower() for n in existing if n}
    base = (base_nickname or "").strip() or "Connection"
    token = (copy_token or "Copy").strip().replace(" ", "-") or "Copy"
    import re

    pattern = re.compile(
        rf"(?:\s*\(\s*{re.escape(copy_token)}(?:\s+\d+)?\s*\)|[-_]+{re.escape(token)}(?:[-_]+\d+)?)\s*$",
        re.IGNORECASE,
    )
    base_clean = pattern.sub("", base).strip() or base

    def unique(name: str) -> bool:
        return name.lower() not in existing_lower

    candidate = f"{base_clean}-{token}"
    if unique(candidate):
        return candidate
    index = 2
    while True:
        candidate = f"{base_clean}-{token}-{index}"
        if unique(candidate):
            return candidate
        index += 1
