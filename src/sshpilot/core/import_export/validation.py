"""Import/export validation reports (GTK-free)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

from ..errors import ErrorCode, FieldError


@dataclass
class ImportValidationReport:
    """Structured result of validating import payload (no UI)."""

    ok: bool
    errors: List[FieldError] = field(default_factory=list)
    warnings: List[FieldError] = field(default_factory=list)
    connection_count: int = 0
    duplicate_nicknames: List[str] = field(default_factory=list)


def validate_connection_export_payload(data: Mapping[str, Any]) -> ImportValidationReport:
    """Validate a connection export / backup fragment for structural sanity."""
    errors: List[FieldError] = []
    warnings: List[FieldError] = []
    if not isinstance(data, Mapping):
        return ImportValidationReport(
            ok=False,
            errors=[FieldError(
                field="root",
                message="Export payload must be a mapping",
                reason="payload_not_mapping",
            )],
        )

    connections = data.get("connections")
    if connections is None and "hosts" in data:
        connections = data.get("hosts")
    if connections is None:
        # SSH config text exports may only carry ssh_config
        if "ssh_config" in data or "config" in data:
            return ImportValidationReport(ok=True, connection_count=0)
        errors.append(
            FieldError(
                field="connections",
                message="Missing connections list",
                reason="connections_list_missing",
            )
        )
        return ImportValidationReport(ok=False, errors=errors)

    if not isinstance(connections, list):
        errors.append(
            FieldError(
                field="connections",
                message="connections must be a list",
                reason="connections_not_list",
            )
        )
        return ImportValidationReport(ok=False, errors=errors)

    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    for idx, item in enumerate(connections):
        if not isinstance(item, Mapping):
            errors.append(
                FieldError(
                    field=f"connections[{idx}]",
                    message="Entry must be a mapping",
                    reason="connection_entry_not_mapping",
                )
            )
            continue
        nick = str(item.get("nickname") or item.get("host") or "").strip()
        if not nick:
            errors.append(
                FieldError(
                    field=f"connections[{idx}].nickname",
                    message="Connection nickname is required",
                    reason="connection_nickname_required",
                )
            )
            continue
        if nick.lower() in seen:
            duplicates.append(nick)
        else:
            seen[nick.lower()] = idx
        if " " in nick:
            errors.append(
                FieldError(
                    field=f"connections[{idx}].nickname",
                    message="Nickname cannot contain whitespace",
                    code=ErrorCode.VALIDATION_ERROR,
                    reason="connection_nickname_whitespace",
                )
            )

    if duplicates:
        warnings.append(
            FieldError(
                field="connections",
                message="Duplicate nicknames in import",
                severity="warning",
                reason="duplicate_nicknames",
            )
        )

    return ImportValidationReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        connection_count=len(connections),
        duplicate_nicknames=duplicates,
    )
