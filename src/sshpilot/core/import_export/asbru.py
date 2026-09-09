"""Ásbrú Connection Manager export → SSH Pilot import adapter (GTK-free).

Accepts the YAML produced by Ásbrú's **Export selected connections** action
(not the live ``asbru.yml`` config). Parsing never mutates SSH Pilot state;
apply happens through :class:`ConnectionApplicationService`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ..errors import CoreError, ErrorCode

_PAC_ROOT_MARKERS = frozenset({"__PAC__EXPORTED__", "__PAC__ROOT__", "__PAC_SHELL__"})
_SSH_METHOD_RE = re.compile(r"ssh|autossh", re.IGNORECASE)
_COPY_SUFFIX_RE = re.compile(r"\s*- copy$", re.IGNORECASE)
_MULTI_DASH_RE = re.compile(r"-{2,}")
_NON_ALIAS_RE = re.compile(r"[^\w.-]+", re.UNICODE)

_FORWARD_PATTERN = re.compile(
    r"(?P<flag>-[LR])\s+"
    r"(?P<spec>(?:\[[^\]]+\]|[^:\s]+)"
    r"(?::(?:\[[^\]]+\]|[^:\s]+)){2,3})"
)


@dataclass(frozen=True)
class AsbruGroupDraft:
    """One group from an Ásbrú export, keyed by the export UUID."""

    source_id: str
    name: str
    parent_source_id: Optional[str] = None
    description: str = ""


@dataclass(frozen=True)
class AsbruConnectionDraft:
    """One SSH connection draft ready for ``CreateConnectionRequest``."""

    source_id: str
    nickname: str
    display_name: str
    hostname: str
    username: str = ""
    port: int = 22
    group_source_id: Optional[str] = None
    proxy_jump: Tuple[str, ...] = ()
    forwarding_rules: Tuple[Dict[str, Any], ...] = ()
    identity_files: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()


@dataclass
class AsbruParseResult:
    """Structured result of parsing an Ásbrú export."""

    groups: List[AsbruGroupDraft] = field(default_factory=list)
    connections: List[AsbruConnectionDraft] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_asbru_export(path: Union[str, Path]) -> AsbruParseResult:
    """Load and parse an Ásbrú export YAML file from disk."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise CoreError(
            ErrorCode.IMPORT_ERROR,
            f"Ásbrú export not found: {file_path}",
            details={"reason": "asbru_export_not_found", "path": str(file_path)},
        )
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoreError(
            ErrorCode.IMPORT_ERROR,
            f"Failed to read Ásbrú export: {exc}",
            details={"reason": "asbru_export_unreadable", "path": str(file_path)},
        ) from exc
    return parse_asbru_export_text(text)


def parse_asbru_export_text(text: str) -> AsbruParseResult:
    """Parse Ásbrú export YAML text into drafts."""
    try:
        import yaml
    except ImportError as exc:
        raise CoreError(
            ErrorCode.IMPORT_ERROR,
            "PyYAML is required to import Ásbrú exports (pip install PyYAML)",
            details={"reason": "pyyaml_missing"},
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CoreError(
            ErrorCode.IMPORT_ERROR,
            f"Invalid Ásbrú YAML: {exc}",
            details={"reason": "asbru_yaml_invalid"},
        ) from exc
    return parse_asbru_export(data)


def parse_asbru_export(data: Any) -> AsbruParseResult:
    """Parse a loaded Ásbrú export mapping into connection/group drafts."""
    result = AsbruParseResult()
    entries = _extract_entries(data, result)
    if result.errors:
        return result
    if not entries:
        result.errors.append(
            "No connections or groups found. Use Ásbrú's "
            "'Export selected connections' (not the live asbru.yml)."
        )
        return result

    groups: List[AsbruGroupDraft] = []
    connections: List[AsbruConnectionDraft] = []
    used_nicknames: set[str] = set()

    for source_id, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        if _is_group(entry):
            draft = _parse_group(source_id, entry)
            if draft is not None:
                groups.append(draft)
            continue

        method = str(entry.get("method") or "SSH")
        if method and not _SSH_METHOD_RE.search(method):
            name = _clean_display_name(entry.get("name") or entry.get("title") or source_id)
            result.warnings.append(
                f"Skipped non-SSH connection {name!r} (method={method!r})"
            )
            continue

        draft, warnings = _parse_connection(source_id, entry, used_nicknames)
        result.warnings.extend(warnings)
        if draft is not None:
            connections.append(draft)
            used_nicknames.add(draft.nickname.casefold())

    # Drop group parents that were not imported (e.g. PAC root markers).
    known_groups = {g.source_id for g in groups}
    normalized_groups: List[AsbruGroupDraft] = []
    for group in groups:
        parent = group.parent_source_id
        if parent is not None and parent not in known_groups:
            parent = None
        normalized_groups.append(
            AsbruGroupDraft(
                source_id=group.source_id,
                name=group.name,
                parent_source_id=parent,
                description=group.description,
            )
        )

    kept_groups, prune_warnings = _prune_empty_groups(normalized_groups, connections)
    result.warnings.extend(prune_warnings)
    result.groups = _order_groups_parents_first(kept_groups)
    result.connections = connections
    if not connections and not result.warnings and not result.errors:
        result.warnings.append("Export contained groups only; no SSH connections imported")
    return result


def sanitize_host_alias(name: str, *, existing: Optional[set[str]] = None) -> str:
    """Turn an Ásbrú connection name into a whitespace-free SSH Host alias."""
    cleaned = _clean_display_name(name)
    alias = _NON_ALIAS_RE.sub("-", cleaned.replace(" ", "-"))
    alias = _MULTI_DASH_RE.sub("-", alias).strip("-._")
    if not alias:
        alias = "host"
    if alias.startswith("-"):
        alias = f"h{alias}"
    existing = existing or set()
    existing_cf = {n.casefold() for n in existing}
    if alias.casefold() not in existing_cf:
        return alias
    index = 2
    while True:
        candidate = f"{alias}-{index}"
        if candidate.casefold() not in existing_cf:
            return candidate
        index += 1


def split_spec_respecting_brackets(spec: str) -> List[str]:
    """Split a colon-separated forward spec, keeping bracketed IPv6 intact."""
    parts: List[str] = []
    cur: List[str] = []
    in_bracket = False
    for ch in spec:
        if ch == "[":
            in_bracket = True
            cur.append(ch)
        elif ch == "]":
            in_bracket = False
            cur.append(ch)
        elif ch == ":" and not in_bracket:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_forwards_from_options(options_str: str) -> List[Dict[str, Any]]:
    """Parse ``-L`` / ``-R`` specs from Ásbrú ``options`` into forwarding rule dicts."""
    if not options_str:
        return []
    rules: List[Dict[str, Any]] = []
    for match in _FORWARD_PATTERN.finditer(options_str):
        flag = match.group("flag")
        parts = split_spec_respecting_brackets(match.group("spec"))
        try:
            if flag == "-L":
                rule = _local_forward_rule(parts)
            else:
                rule = _remote_forward_rule(parts)
        except (TypeError, ValueError):
            continue
        if rule is not None:
            rules.append(rule)
    return rules


def build_proxy_jump(entry: Mapping[str, Any]) -> Tuple[str, ...]:
    """Build a ProxyJump destination tuple from Ásbrú jump fields."""
    jump_ip = (
        entry.get("jump ip")
        or entry.get("jump_ip")
        or entry.get("jump")
        or entry.get("ProxyJump")
    )
    if not jump_ip:
        return ()
    jump_ip = str(jump_ip).strip()
    if not jump_ip:
        return ()
    jump_user = entry.get("jump user") or entry.get("jump_user")
    jump_port = entry.get("jump port") or entry.get("jump_port")
    destination = f"{jump_user}@{jump_ip}" if jump_user else jump_ip
    if jump_port and str(jump_port).strip() and str(jump_port).strip() != "22":
        destination = f"{destination}:{jump_port}"
    return (destination,)


# -- internals ---------------------------------------------------------------


def _extract_entries(
    data: Any, result: AsbruParseResult
) -> Dict[str, Mapping[str, Any]]:
    if data is None:
        result.errors.append("Ásbrú export is empty")
        return {}
    if not isinstance(data, Mapping):
        result.errors.append("Ásbrú export must be a YAML mapping")
        return {}

    # Full asbru.yml wraps connections under ``environments``.
    if "environments" in data and isinstance(data.get("environments"), Mapping):
        result.warnings.append(
            "Parsed the environments section from a full Ásbrú config; "
            "prefer 'Export selected connections' for a clean import"
        )
        raw = data["environments"]
    else:
        raw = data

    entries: Dict[str, Mapping[str, Any]] = {}
    for key, value in raw.items():
        source_id = str(key)
        if source_id in _PAC_ROOT_MARKERS:
            continue
        if not isinstance(value, Mapping):
            # Children maps in raw asbru.yml are ``uuid: 1`` ints — skip quietly.
            continue
        if "_is_group" not in value and "ip" not in value and "name" not in value:
            continue
        entries[source_id] = value

    if not entries and any(
        isinstance(v, Mapping) and ("defaults" in data or "tmp" in data)
        for v in (data,)
    ):
        result.errors.append(
            "This looks like a live asbru.yml, not an export. "
            "In Ásbrú, select connections → right-click → Export."
        )
    return entries


def _is_group(entry: Mapping[str, Any]) -> bool:
    flag = entry.get("_is_group")
    if flag in (1, "1", True):
        return True
    if flag in (0, "0", False):
        return False
    # Some exports omit the flag for folders that only have children.
    return bool(entry.get("children")) and not entry.get("ip")


def _parse_group(source_id: str, entry: Mapping[str, Any]) -> Optional[AsbruGroupDraft]:
    name = _clean_display_name(entry.get("name") or entry.get("title") or source_id)
    if not name:
        return None
    parent = _normalize_parent(entry.get("parent"))
    description = str(entry.get("description") or "").strip()
    return AsbruGroupDraft(
        source_id=source_id,
        name=name,
        parent_source_id=parent,
        description=description,
    )


def _parse_connection(
    source_id: str,
    entry: Mapping[str, Any],
    used_nicknames: set[str],
) -> Tuple[Optional[AsbruConnectionDraft], List[str]]:
    warnings: List[str] = []
    display_name = _clean_display_name(entry.get("name") or entry.get("title") or source_id)
    if not display_name:
        warnings.append(f"Skipped connection {source_id}: missing name")
        return None, warnings

    hostname = str(entry.get("ip") or entry.get("host") or entry.get("hostname") or "").strip()
    if not hostname:
        warnings.append(f"Skipped connection {display_name!r}: missing host/IP")
        return None, warnings

    nickname = sanitize_host_alias(display_name, existing=used_nicknames)
    username = _resolve_username(entry)
    port = _resolve_port(entry)
    group_id = _normalize_parent(entry.get("parent"))

    options = str(entry.get("options") or "")
    forwarding_rules = tuple(parse_forwards_from_options(options))
    proxy_jump = build_proxy_jump(entry)
    identity_files = _resolve_identity_files(entry)

    conn_warnings: List[str] = []
    if entry.get("expect") or entry.get("macros") or entry.get("variables"):
        conn_warnings.append(
            f"{display_name!r}: Ásbrú expect/macros/variables are not imported"
        )
    if nickname != display_name.replace(" ", "-") and " " in display_name:
        conn_warnings.append(
            f"Renamed {display_name!r} → Host alias {nickname!r}"
        )

    draft = AsbruConnectionDraft(
        source_id=source_id,
        nickname=nickname,
        display_name=display_name,
        hostname=hostname,
        username=username,
        port=port,
        group_source_id=group_id,
        proxy_jump=proxy_jump,
        forwarding_rules=forwarding_rules,
        identity_files=identity_files,
        warnings=tuple(conn_warnings),
    )
    warnings.extend(conn_warnings)
    return draft, warnings


def _resolve_username(entry: Mapping[str, Any]) -> str:
    # Prefer the SSH login user. ``passphrase user`` is for key auth identity and
    # was incorrectly used as the primary username by the community converter.
    for key in ("user", "login", "username", "passphrase user", "passphrase_user"):
        value = entry.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _resolve_identity_files(entry: Mapping[str, Any]) -> Tuple[str, ...]:
    """Map Ásbrú key path fields onto OpenSSH IdentityFile values.

    Ásbrú's ``public key`` / ``public_key`` field is the path passed to ssh
    ``-i`` (typically the private key path, despite the name).
    """
    raw = (
        entry.get("public key")
        or entry.get("public_key")
        or entry.get("identity_file")
        or entry.get("IdentityFile")
    )
    if raw is None:
        return ()
    path = str(raw).strip()
    if not path:
        return ()
    return (path,)


def _resolve_port(entry: Mapping[str, Any]) -> int:
    raw = entry.get("port")
    if raw is None or raw == "":
        return 22
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return 22
    if 1 <= port <= 65535:
        return port
    return 22


def _normalize_parent(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in _PAC_ROOT_MARKERS:
        return None
    return text


def _clean_display_name(value: Any) -> str:
    if value is None:
        return ""
    return _COPY_SUFFIX_RE.sub("", str(value)).strip()


def _order_groups_parents_first(groups: Sequence[AsbruGroupDraft]) -> List[AsbruGroupDraft]:
    by_id = {g.source_id: g for g in groups}
    ordered: List[AsbruGroupDraft] = []
    visiting: set[str] = set()
    seen: set[str] = set()

    def visit(gid: str) -> None:
        if gid in seen or gid not in by_id:
            return
        if gid in visiting:
            return
        visiting.add(gid)
        parent = by_id[gid].parent_source_id
        if parent:
            visit(parent)
        visiting.discard(gid)
        seen.add(gid)
        ordered.append(by_id[gid])

    for group in groups:
        visit(group.source_id)
    return ordered


def _prune_empty_groups(
    groups: Sequence[AsbruGroupDraft],
    connections: Sequence[AsbruConnectionDraft],
) -> Tuple[List[AsbruGroupDraft], List[str]]:
    """Keep only groups that contain (or ancestor) an imported SSH connection."""
    by_id = {g.source_id: g for g in groups}
    keep: set[str] = set()
    for conn in connections:
        gid = conn.group_source_id
        while gid and gid in by_id:
            if gid in keep:
                break
            keep.add(gid)
            gid = by_id[gid].parent_source_id

    warnings: List[str] = []
    kept: List[AsbruGroupDraft] = []
    for group in groups:
        if group.source_id in keep:
            kept.append(group)
        else:
            warnings.append(
                f"Skipped empty group {group.name!r} (no importable SSH connections)"
            )
    return kept, warnings


def _local_forward_rule(parts: List[str]) -> Optional[Dict[str, Any]]:
    if len(parts) == 4:
        bind, listen_port, remote_host, remote_port = parts
        return {
            "type": "local",
            "listen_addr": bind,
            "listen_port": int(listen_port),
            "remote_host": remote_host,
            "remote_port": int(remote_port),
            "enabled": True,
        }
    if len(parts) == 3:
        listen_port, remote_host, remote_port = parts
        return {
            "type": "local",
            "listen_addr": "127.0.0.1",
            "listen_port": int(listen_port),
            "remote_host": remote_host,
            "remote_port": int(remote_port),
            "enabled": True,
        }
    return None


def _remote_forward_rule(parts: List[str]) -> Optional[Dict[str, Any]]:
    if len(parts) == 4:
        bind, listen_port, local_host, local_port = parts
        return {
            "type": "remote",
            "listen_addr": bind,
            "listen_port": int(listen_port),
            "local_host": local_host,
            "local_port": int(local_port),
            "enabled": True,
        }
    if len(parts) == 3:
        listen_port, local_host, local_port = parts
        return {
            "type": "remote",
            "listen_addr": "",
            "listen_port": int(listen_port),
            "local_host": local_host,
            "local_port": int(local_port),
            "enabled": True,
        }
    return None
