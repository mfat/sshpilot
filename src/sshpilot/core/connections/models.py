"""GTK-free connection and group domain models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional
import copy
import re


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
    # Persistence metadata owned by the daemon: the source file the record
    # was loaded from and the per-connection mutation generation used for
    # stale-editor detection. Both are internal; clients never supply them.
    source: str = ""
    generation: int = 0
    # Identity remains the SSH ``Host`` alias (``id``/``nickname``). ``host``
    # is the first Host token and ``aliases`` the remaining tokens (or empty
    # when the host is the alias itself). No UUID fields are produced.
    host: str = ""
    aliases: tuple = ()
    # Literal parser evidence used only by the identity prototype. These are
    # not serialized as public connection fields and do not become SSH IDs.
    raw_port: Optional[str] = None
    raw_username: Optional[str] = None
    raw_identity_files: tuple = ()

    def normalized_nickname(self) -> str:
        return (self.nickname or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        payload = copy.deepcopy(self.data) if self.data else {}
        payload.pop("uuid", None)
        payload.update(
            {
                "id": self.nickname,
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
        raw_port = raw.pop("__identity_raw_port", None)
        raw_username = raw.pop("__identity_raw_username", None)
        raw_identity_files = raw.pop("__identity_raw_identity_files", ())
        raw.pop("uuid", None)
        nick = str(raw.get("nickname") or raw.get("id") or raw.get("host") or "").strip()
        cid = connection_id or str(raw.get("id") or nick).strip()
        raw_aliases = raw.get("aliases") or raw.get("__host_tokens") or ()
        if isinstance(raw_aliases, (tuple, list)):
            aliases = tuple(
                str(a) for a in raw_aliases if a and str(a) != nick
            )
        else:
            aliases = ()
        if not cid:
            cid = nick
        try:
            port = int(raw.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        try:
            generation = int(raw.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        return cls(
            id=cid,
            nickname=nick or cid,
            hostname=str(raw.get("hostname") or raw.get("host") or "").strip(),
            username=str(raw.get("username") or "").strip(),
            port=port,
            protocol=str(raw.get("protocol") or "ssh").strip() or "ssh",
            group_id=(str(raw["group_id"]) if raw.get("group_id") else None),
            order=int(raw.get("order") or 0),
            data=raw,
            source=str(raw.get("source") or "").strip(),
            generation=generation,
            host=str(raw.get("host") or nick or cid).strip(),
            aliases=aliases,
            raw_port=(str(raw_port) if raw_port is not None else None),
            raw_username=(str(raw_username) if raw_username is not None else None),
            raw_identity_files=(
                tuple(str(value) for value in raw_identity_files)
                if isinstance(raw_identity_files, (tuple, list))
                else ()
            ),
        )

    def with_updates(self, updates: Mapping[str, Any]) -> "ConnectionRecord":
        merged = copy.deepcopy(self.data)
        merged.update(dict(updates))
        merged.pop("uuid", None)
        new_nick = str(updates.get("nickname", self.nickname)).strip() or self.nickname
        merged["id"] = new_nick
        merged["nickname"] = new_nick
        return ConnectionRecord.from_dict(merged, connection_id=new_nick)


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


def generate_group_slug(name: str, existing_ids: set[str]) -> str:
    """Generate a stable, unique group ID slug from a display name."""
    clean = (name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]", "-", clean)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "group"
    if slug not in existing_ids:
        return slug
    index = 2
    while True:
        candidate = f"{slug}-{index}"
        if candidate not in existing_ids:
            return candidate
        index += 1
