"""Import/export orchestration (GTK-free).

File chooser dialogs and notifications remain in GTK. Core accepts paths,
streams, or serializable objects and never opens dialogs.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, TextIO, Union

from ..errors import CoreError, ErrorCode, FieldError
from .validation import validate_connection_export_payload

CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


class MergeStrategy(str, Enum):
    REPLACE = "replace"
    MERGE = "merge"
    SKIP = "skip"


class ConflictAction(str, Enum):
    KEEP_EXISTING = "keep_existing"
    TAKE_IMPORTED = "take_imported"
    SKIP = "skip"
    RENAME_IMPORTED = "rename_imported"


@dataclass(frozen=True)
class ConflictDescription:
    kind: str  # connection | group
    key: str
    message: str
    suggested_action: ConflictAction = ConflictAction.SKIP


@dataclass
class ImportPlan:
    """Dry-run plan for an import."""

    ok: bool
    schema_version: int
    strategy: MergeStrategy
    connections_to_add: List[str] = field(default_factory=list)
    connections_to_update: List[str] = field(default_factory=list)
    connections_to_skip: List[str] = field(default_factory=list)
    groups_to_add: List[str] = field(default_factory=list)
    groups_conflicting: List[str] = field(default_factory=list)
    conflicts: List[ConflictDescription] = field(default_factory=list)
    warnings: List[FieldError] = field(default_factory=list)
    errors: List[FieldError] = field(default_factory=list)
    secrets_included: bool = False
    unsupported_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "schema_version": self.schema_version,
            "strategy": self.strategy.value,
            "connections_to_add": list(self.connections_to_add),
            "connections_to_update": list(self.connections_to_update),
            "connections_to_skip": list(self.connections_to_skip),
            "groups_to_add": list(self.groups_to_add),
            "groups_conflicting": list(self.groups_conflicting),
            "conflicts": [
                {
                    "kind": c.kind,
                    "key": c.key,
                    "message": c.message,
                    "suggested_action": c.suggested_action.value,
                }
                for c in self.conflicts
            ],
            "warnings": [w.message for w in self.warnings],
            "errors": [e.message for e in self.errors],
            "secrets_included": self.secrets_included,
            "unsupported_fields": list(self.unsupported_fields),
        }


@dataclass
class ImportResult:
    ok: bool
    plan: ImportPlan
    applied: bool = False
    message: str = ""
    partial_failures: List[str] = field(default_factory=list)


def migrate_payload(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Migrate older schemas forward. Unknown future schemas raise."""
    if not isinstance(data, Mapping):
        raise CoreError(ErrorCode.IMPORT_ERROR, "Payload must be a mapping")
    version = data.get("version", data.get("schema_version", CURRENT_SCHEMA_VERSION))
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise CoreError(ErrorCode.IMPORT_ERROR, f"Invalid schema version: {version}") from exc
    if version > CURRENT_SCHEMA_VERSION:
        raise CoreError(
            ErrorCode.IMPORT_ERROR,
            f"Unsupported future schema version: {version}",
            details={"version": version},
        )
    if version < 1:
        raise CoreError(ErrorCode.IMPORT_ERROR, f"Unsupported schema version: {version}")
    payload = dict(data)
    payload["version"] = CURRENT_SCHEMA_VERSION
    # v1 is current — no transform yet. Hook retained for future migrations.
    return payload


def _existing_nicknames(existing: Optional[Sequence[Mapping[str, Any]]]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for item in existing or ():
        if not isinstance(item, Mapping):
            continue
        nick = str(item.get("nickname") or item.get("host") or "").strip().lower()
        if nick:
            out[nick] = item
    return out


def plan_import(
    payload: Mapping[str, Any],
    *,
    existing_connections: Optional[Sequence[Mapping[str, Any]]] = None,
    existing_groups: Optional[Mapping[str, Any]] = None,
    strategy: MergeStrategy = MergeStrategy.MERGE,
    include_secrets: bool = False,
) -> ImportPlan:
    """Build a dry-run import plan without mutating state."""
    errors: List[FieldError] = []
    warnings: List[FieldError] = []
    try:
        data = migrate_payload(payload)
    except CoreError as exc:
        return ImportPlan(
            ok=False,
            schema_version=int(payload.get("version") or 0) if isinstance(payload, Mapping) else 0,
            strategy=strategy,
            errors=[FieldError(field="version", message=str(exc.message or exc))],
        )

    report = validate_connection_export_payload(data)
    errors.extend(report.errors)
    warnings.extend(report.warnings)

    secrets_present = bool(data.get("credentials") or data.get("secrets"))
    if secrets_present and not include_secrets:
        warnings.append(
            FieldError(
                field="credentials",
                message="Secrets present in payload but excluded by default",
                severity="warning",
            )
        )

    unsupported = [
        key
        for key in data.keys()
        if key
        not in {
            "version",
            "schema_version",
            "connections",
            "hosts",
            "groups",
            "connection_groups",
            "app_config",
            "ssh_config",
            "ssh_config_files",
            "known_hosts",
            "credentials",
            "secrets",
            "private_keys",
            "export_date",
            "format",
            "isolated_mode",
            "config_version",
            "source_home",
            "ssh_config_main_rel",
            "ssh_config_root",
        }
    ]

    existing = _existing_nicknames(existing_connections)
    connections = data.get("connections") or data.get("hosts") or []
    to_add: List[str] = []
    to_update: List[str] = []
    to_skip: List[str] = []
    conflicts: List[ConflictDescription] = []

    if isinstance(connections, list):
        for item in connections:
            if not isinstance(item, Mapping):
                continue
            nick = str(item.get("nickname") or item.get("host") or "").strip()
            if not nick:
                continue
            key = nick.lower()
            if key in existing:
                if strategy == MergeStrategy.SKIP:
                    to_skip.append(nick)
                elif strategy == MergeStrategy.REPLACE:
                    to_update.append(nick)
                else:  # MERGE
                    conflicts.append(
                        ConflictDescription(
                            kind="connection",
                            key=nick,
                            message=f"Connection {nick!r} already exists",
                            suggested_action=ConflictAction.TAKE_IMPORTED,
                        )
                    )
                    to_update.append(nick)
            else:
                to_add.append(nick)

    groups_blob = data.get("groups") or data.get("connection_groups") or {}
    groups_to_add: List[str] = []
    groups_conflicting: List[str] = []
    existing_group_ids = set()
    if isinstance(existing_groups, Mapping):
        raw = existing_groups.get("groups") if "groups" in existing_groups else existing_groups
        if isinstance(raw, Mapping):
            existing_group_ids = {str(k) for k in raw.keys()}
    if isinstance(groups_blob, Mapping):
        raw_groups = groups_blob.get("groups") if "groups" in groups_blob else groups_blob
        if isinstance(raw_groups, Mapping):
            for gid, gdata in raw_groups.items():
                name = (
                    str(gdata.get("name") or gid)
                    if isinstance(gdata, Mapping)
                    else str(gid)
                )
                if str(gid) in existing_group_ids:
                    groups_conflicting.append(name)
                    conflicts.append(
                        ConflictDescription(
                            kind="group",
                            key=str(gid),
                            message=f"Group {name!r} already exists",
                            suggested_action=ConflictAction.KEEP_EXISTING,
                        )
                    )
                else:
                    groups_to_add.append(name)

    ok = not errors
    return ImportPlan(
        ok=ok,
        schema_version=int(data.get("version") or CURRENT_SCHEMA_VERSION),
        strategy=strategy,
        connections_to_add=to_add,
        connections_to_update=to_update,
        connections_to_skip=to_skip,
        groups_to_add=groups_to_add,
        groups_conflicting=groups_conflicting,
        conflicts=conflicts,
        warnings=warnings,
        errors=errors,
        secrets_included=bool(include_secrets and secrets_present),
        unsupported_fields=unsupported,
    )


def apply_import_to_connection_dicts(
    payload: Mapping[str, Any],
    existing: Sequence[Mapping[str, Any]],
    *,
    strategy: MergeStrategy = MergeStrategy.MERGE,
    include_secrets: bool = False,
) -> ImportResult:
    """Apply import over plain connection dicts (no GTK / no SSH config I/O)."""
    plan = plan_import(
        payload,
        existing_connections=existing,
        strategy=strategy,
        include_secrets=include_secrets,
    )
    if not plan.ok:
        return ImportResult(ok=False, plan=plan, applied=False, message="Validation failed")

    data = migrate_payload(payload)
    incoming = data.get("connections") or data.get("hosts") or []
    by_nick = _existing_nicknames(existing)
    result_list: List[Dict[str, Any]] = [dict(x) for x in existing if isinstance(x, Mapping)]
    partial: List[str] = []

    if strategy == MergeStrategy.REPLACE:
        result_list = []
        by_nick = {}

    if isinstance(incoming, list):
        for item in incoming:
            if not isinstance(item, Mapping):
                partial.append("non-mapping connection entry skipped")
                continue
            nick = str(item.get("nickname") or item.get("host") or "").strip()
            if not nick:
                partial.append("connection missing nickname skipped")
                continue
            key = nick.lower()
            if key in by_nick and strategy == MergeStrategy.SKIP:
                continue
            cleaned = {k: v for k, v in item.items() if k not in {"password", "secret"}}
            if not include_secrets:
                cleaned.pop("credentials", None)
            if key in by_nick and strategy in {MergeStrategy.MERGE, MergeStrategy.REPLACE}:
                # Replace matching nickname entry
                for idx, cur in enumerate(result_list):
                    cur_nick = str(cur.get("nickname") or cur.get("host") or "").strip().lower()
                    if cur_nick == key:
                        merged = dict(cur)
                        merged.update(cleaned)
                        result_list[idx] = merged
                        break
                else:
                    result_list.append(dict(cleaned))
            else:
                result_list.append(dict(cleaned))
                by_nick[key] = cleaned

    # Stash result on plan detail for callers
    plan_with = plan
    return ImportResult(
        ok=True,
        plan=plan_with,
        applied=True,
        message=f"Imported {len(incoming) if isinstance(incoming, list) else 0} entries",
        partial_failures=partial,
    )


def load_payload(source: Union[str, os.PathLike[str], TextIO, Mapping[str, Any]]) -> Dict[str, Any]:
    """Load a JSON payload from path, stream, or mapping."""
    if isinstance(source, Mapping):
        return {str(k): v for k, v in source.items()}
    if isinstance(source, (str, os.PathLike)):
        path = Path(str(source))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CoreError(ErrorCode.IMPORT_ERROR, f"Import file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise CoreError(ErrorCode.IMPORT_ERROR, f"Malformed JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CoreError(ErrorCode.IMPORT_ERROR, "Payload must be a JSON object")
        return data
    data = json.load(source)
    if not isinstance(data, dict):
        raise CoreError(ErrorCode.IMPORT_ERROR, "Payload must be a JSON object")
    return data


def export_connections_payload(
    connections: Sequence[Mapping[str, Any]],
    *,
    groups: Optional[Mapping[str, Any]] = None,
    include_secrets: bool = False,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a serializable export payload. Secrets excluded by default."""
    cleaned = []
    for item in connections:
        if not isinstance(item, Mapping):
            continue
        entry = {
            k: v
            for k, v in item.items()
            if not str(k).startswith("__") and k not in {"password", "password_changed"}
        }
        if not include_secrets:
            entry.pop("credentials", None)
            entry.pop("secret", None)
        cleaned.append(entry)
    payload: Dict[str, Any] = {
        "version": CURRENT_SCHEMA_VERSION,
        "connections": cleaned,
    }
    if groups is not None:
        payload["groups"] = dict(groups)
    if extra:
        for key, value in extra.items():
            if key not in payload:
                payload[key] = value
    if include_secrets and extra and "credentials" in extra:
        payload["credentials"] = extra["credentials"]
    return payload


def atomic_write_json(path: Union[str, os.PathLike], payload: Mapping[str, Any]) -> None:
    """Atomically write JSON via temp file + replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(payload), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
